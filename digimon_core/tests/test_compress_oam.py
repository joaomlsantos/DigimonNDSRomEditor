"""Compress OAM: re-cover an existing BTCHR sprite with occupied-only
coverage (btchrspr.compress_existing) — same pixels, smaller footprint_scale.

Verified against the real BTCHR.PAK: a known sparse sprite shrinks, pixels
are preserved exactly, a boundary-256 boss decodes at the right stride, dense
sprites don't lose pixels, and the op is stable (re-compressing is a no-op).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import btchr, btchrspr, fnt, ncer, pak, rom, sprite  # noqa: E402

ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
DUSK_US = os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds")

TOUCANMON = 252  # biggest saver: fs 256 -> 90 (spread wings, empty box)


def _load_btchr_pak():
    rom_bytes = bytes(rom.loadRom(DUSK_US))
    ft = fnt.FileTable.from_rom(rom_bytes)
    start, end = ft.resolve("DAT/BTCHR.PAK")
    return pak.PakFile(rom_bytes[start:end])


def _group_entries(p, gi):
    return [bytes(p.entries[gi * btchr.GROUP_SIZE + i]) for i in range(btchr.GROUP_SIZE)]


def _visible_pixels(entries):
    """Per-cell set of opaque pixels + colour, normalised to each cell's
    content bbox so canvas-offset differences don't register — this is the
    ground truth 'does it look the same' comparison."""
    tiles, *_ = sprite.parse_ncgr(sprite.decompress_rle30(entries[1]))
    pals, _ = sprite.parse_nclr(sprite.decompress_rle30(entries[2]))
    parsed = ncer.parse_ncer(sprite.decompress_rle30(entries[3]))
    out = []
    for c in parsed.cells:
        buf, w, h = btchr.render_cell_rgba(
            c, tiles, pals[0], boundary_bytes=parsed.boundary_bytes
        )
        opq = [(x, y) for y in range(h) for x in range(w) if buf[(y * w + x) * 4 + 3]]
        if not opq:
            out.append(frozenset())
            continue
        x0 = min(p[0] for p in opq)
        y0 = min(p[1] for p in opq)
        out.append(frozenset(
            (x - x0, y - y0, buf[(y * w + x) * 4], buf[(y * w + x) * 4 + 1],
             buf[(y * w + x) * 4 + 2])
            for (x, y) in opq
        ))
    return out


def _visible_pixels_abs(entries):
    """Like _visible_pixels but at ABSOLUTE OAM coords — catches a position
    shift the bbox-normalised compare would miss (an earlier centred re-cover
    silently moved sprites in-game)."""
    tiles, *_ = sprite.parse_ncgr(sprite.decompress_rle30(entries[1]))
    pals, _ = sprite.parse_nclr(sprite.decompress_rle30(entries[2]))
    parsed = ncer.parse_ncer(sprite.decompress_rle30(entries[3]))
    out = []
    for c in parsed.cells:
        xmin, ymin, _, _ = btchr.cell_bbox(c)
        buf, w, h = btchr.render_cell_rgba(
            c, tiles, pals[0], boundary_bytes=parsed.boundary_bytes
        )
        out.append(frozenset(
            (xmin + x, ymin + y, buf[(y * w + x) * 4], buf[(y * w + x) * 4 + 1],
             buf[(y * w + x) * 4 + 2])
            for y in range(h) for x in range(w) if buf[(y * w + x) * 4 + 3]
        ))
    return out


def _cells_share_structure(entries):
    """True iff every cell has the SAME OAM count, shapes and tile-order (only
    positions may vary). The engine renders every frame with cell 0's OBJ
    structure — Tsumemon g17 varies per-cell *positions* and renders fine, but a
    per-cell *structure* garbles frames 1-4 in-game (ChaosGallantmon,
    Ophanimon). So compressed output must keep all cells structurally identical."""
    parsed = ncer.parse_ncer(sprite.decompress_rle30(entries[3]))
    cells = parsed.cells
    if not cells:
        return True
    tiles, *_ = sprite.parse_ncgr(sprite.decompress_rle30(entries[1]))
    st = max(1, parsed.boundary_bytes // 64)
    fs_slots = (len(tiles) // 64) // len(cells) // st
    ref = [(o.w, o.h, o.tile) for o in cells[0].oams]
    for i, c in enumerate(cells):
        sig = [(o.w, o.h, o.tile - i * fs_slots) for o in c.oams]
        if sig != ref:
            return False
    return True


@unittest.skipUnless(os.path.exists(DUSK_US), "needs Dusk US ROM")
class CompressExistingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pak = _load_btchr_pak()

    def test_sparse_sprite_shrinks_and_preserves_pixels(self):
        entries = _group_entries(self.pak, TOUCANMON)
        spr, old_fs, new_fs = btchrspr.compress_existing(entries)
        self.assertLess(new_fs, old_fs, "sparse sprite should reclaim tiles")
        self.assertEqual(
            _visible_pixels(entries), _visible_pixels(list(spr.entries)),
            "compressed sprite must render identical pixels",
        )
        # tpf/chrsize/btchrsize all follow the new fs
        self.assertEqual(spr.source_tpf, new_fs)

    def test_dense_sprite_round_trips_pixels(self):
        # a sprite that gains nothing must still not corrupt (caller's Δ<=0
        # guard skips applying it, but the rebuild must be lossless).
        entries = _group_entries(self.pak, 1)
        spr, _old, _new = btchrspr.compress_existing(entries)
        self.assertEqual(
            _visible_pixels(entries), _visible_pixels(list(spr.entries))
        )

    def test_boundary256_boss_decodes_at_right_stride(self):
        gi256 = next(
            (gi for gi in range(btchr.parse_pak_groups(self.pak))
             if ncer.parse_ncer(
                 sprite.decompress_rle30(self.pak.entries[gi * 5 + 3])
             ).boundary_bytes == 256),
            None,
        )
        self.assertIsNotNone(gi256, "expected a boundary-256 group")
        entries = _group_entries(self.pak, gi256)
        try:
            spr, _o, _n = btchrspr.compress_existing(entries)
        except ValueError:
            self.skipTest("this boss overflows the occupied re-cover")
        self.assertEqual(
            _visible_pixels(entries), _visible_pixels(list(spr.entries)),
            "boundary-256 source must decode at its own stride, not 128",
        )

    def test_compress_preserves_absolute_position(self):
        # compress pins the OAMs to the sprite's OWN origin (not centred), so
        # the sprite stays exactly where it was — a centred re-cover would move
        # it in-game, which the normalised pixel compare can't detect.
        entries = _group_entries(self.pak, TOUCANMON)
        spr, _o, _n = btchrspr.compress_existing(entries)
        self.assertEqual(
            _visible_pixels_abs(entries), _visible_pixels_abs(list(spr.entries)),
            "compressed sprite must render at the same absolute position",
        )

    def test_tall_sprite_recovers_in_range_and_lossless(self):
        # Regression: a tall boss laid out un-centred (content dips below
        # OAM-y=127) used to decline with a 'position range' error, because the
        # greedy put a small OBJ at an unencodable row. The cover now anchors a
        # taller OBJ in range to reach that content, so these compress —
        # pixels AND absolute position preserved, every OAM encodable.
        for gi in (409, 411):  # Spinomon, Gaiomon (real battle sprite)
            with self.subTest(group=gi):
                entries = _group_entries(self.pak, gi)
                spr, old_fs, new_fs = btchrspr.compress_existing(entries)
                self.assertLess(new_fs, old_fs)
                self.assertEqual(
                    _visible_pixels_abs(entries),
                    _visible_pixels_abs(list(spr.entries)),
                )
                parsed = ncer.parse_ncer(
                    sprite.decompress_rle30(spr.entries[3])
                )
                for c in parsed.cells:
                    for o in c.oams:
                        self.assertTrue(-256 <= o.x <= 255 and -128 <= o.y <= 127,
                                        f"OAM out of range: x={o.x} y={o.y}")

    def test_declines_are_only_empty_or_capacity(self):
        # After the range-aware cover, the only groups that still decline are
        # empty placeholders and sprites whose opaque content genuinely
        # overflows the 1024-slot OAM tile field — never a 'position range'
        # failure (fixed) and never the fresh-import 'shrink the artwork' text.
        declines = []
        for gi in range(btchr.parse_pak_groups(self.pak)):
            try:
                btchrspr.compress_existing(_group_entries(self.pak, gi))
            except ValueError as exc:
                declines.append(str(exc))
        self.assertTrue(declines, "expected the empty stubs / giants to decline")
        self.assertFalse(
            any("shrink the artwork" in m for m in declines),
            "must not surface the fresh-import size error for a re-cover",
        )
        self.assertFalse(
            any("position range" in m for m in declines),
            "the tall-sprite position-range failure is fixed — none should remain",
        )
        self.assertTrue(
            all(("empty" in m or "overflow" in m or "128" in m) for m in declines),
            "every remaining decline is empty / tile-field / OAM-count",
        )

    def test_compressed_cells_share_oam_structure(self):
        # The safety invariant behind the union approach: the engine draws every
        # frame with cell 0's OBJ structure, so all cells must share OAM
        # count/shapes/tile-order (a per-cell structure garbled Ophanimon/
        # ChaosGallantmon in-game). Guards against re-introducing per-cell cover.
        for gi in (252, 402, 411, 412, 1):  # sparse, two 512-line bosses, dense
            with self.subTest(group=gi):
                entries = _group_entries(self.pak, gi)
                spr, _o, _n = btchrspr.compress_existing(entries)
                self.assertTrue(
                    _cells_share_structure(list(spr.entries)),
                    "compressed cells must be structurally identical",
                )

    def test_joint_compress_lossless_shared_and_beats_union(self):
        # Joint shared-shape cover: all cells share one shape-sequence but
        # position the blocks per frame (Tsumemon-legal), so fs drops toward the
        # biggest single frame. Must be lossless, structurally shared, and never
        # worse than the union cover.
        for gi in (402, 411, 412, 409):  # animated bosses
            with self.subTest(group=gi):
                entries = _group_entries(self.pak, gi)
                _uspr, _ou, un = btchrspr.compress_existing(entries)
                jspr, _oj, jn = btchrspr.compress_existing_joint(entries)
                self.assertEqual(
                    _visible_pixels_abs(entries),
                    _visible_pixels_abs(list(jspr.entries)),
                    "joint cover must render identical pixels at the same place",
                )
                self.assertTrue(
                    _cells_share_structure(list(jspr.entries)),
                    "joint cells must share OAM count/shapes/tile-order",
                )
                self.assertLessEqual(jn, un, "joint must not exceed the union fs")
                # OAM count must stay within the per-screen OBJ budget (max
                # vanilla = 34) or a busy screen (EXP tab) drops the tail OAMs
                # → gaps on the sprite.
                jcells = ncer.parse_ncer(
                    sprite.decompress_rle30(jspr.entries[3])
                ).cells
                self.assertLessEqual(
                    max(len(c.oams) for c in jcells), ncer._JOINT_OAM_BUDGET,
                    "joint OAM count must fit the per-screen OBJ budget",
                )

    def test_joint_positions_vary_per_cell(self):
        # The whole point: the joint cover positions blocks differently per
        # frame. An animated boss's frames must not all be identical (that would
        # mean it degenerated to the union).
        entries = _group_entries(self.pak, 411)  # Gaiomon (arms-raised frame)
        spr, _o, _n = btchrspr.compress_existing_joint(entries)
        cells = ncer.parse_ncer(sprite.decompress_rle30(spr.entries[3])).cells
        pos0 = [(o.x, o.y) for o in cells[0].oams]
        self.assertTrue(
            any([(o.x, o.y) for o in c.oams] != pos0 for c in cells[1:]),
            "joint cover should position blocks per frame",
        )

    def test_fit_is_lossless_when_it_already_fits(self):
        # A sprite that unions to <=512 must come back min_opaque=1, 0 dropped,
        # and byte-identical to the plain union compress (no trimming).
        entries = _group_entries(self.pak, TOUCANMON)
        spr, old_fs, new_fs, mo, dropped = btchrspr.compress_existing_fit(entries)
        self.assertLessEqual(new_fs, 512)
        self.assertEqual((mo, dropped), (1, 0))
        base, _o, _n = btchrspr.compress_existing(entries)
        self.assertEqual(list(spr.entries), list(base.entries))

    def test_fit_trims_only_when_needed_to_reach_512(self):
        # OphanimonC (union 524) must trim a tiny number of faint pixels to land
        # <=512; Gaiomon likewise. The trim must be real (min_opaque>1, some
        # pixels dropped) and the result must fit.
        for gi in (402, 411):
            with self.subTest(group=gi):
                entries = _group_entries(self.pak, gi)
                spr, old_fs, new_fs, mo, dropped = btchrspr.compress_existing_fit(entries)
                self.assertLessEqual(new_fs, 512)
                self.assertGreater(mo, 1)
                self.assertGreater(dropped, 0)
                # trim is small relative to the sprite — a fringe, not a feature
                self.assertLess(dropped, 200)
                self.assertTrue(_cells_share_structure(list(spr.entries)))

    def test_min_opaque_default_matches_lossless(self):
        # occupied_tile_mask(min_opaque=1) must be exactly the old behavior, so
        # the default compress is unchanged.
        idx = bytes([0, 5] + [0] * 62)  # exactly ONE opaque pixel in the 8×8 tile
        occ1, _, _ = ncer.occupied_tile_mask(idx, 8, 8, 1)
        occ2, _, _ = ncer.occupied_tile_mask(idx, 8, 8, 2)
        self.assertTrue(occ1[0][0])   # 1 opaque pixel counts at min_opaque=1
        self.assertFalse(occ2[0][0])  # ...but is trimmed at min_opaque=2

    def test_analyze_oam_cover_is_a_covering_shared_layout(self):
        # The OAM map builds a shared, tile-aligned cover of the UNION (not cell
        # 0's raw OAMs), so the read-only map and the editor agree and Apply is
        # lossless: boxes cover every opaque union tile, fill is 0..1, slots sum
        # to fs, and stored_fs reports the on-disk footprint separately.
        d = btchr.decode_digimon(self.pak, TOUCANMON)
        an = btchr.analyze_oam_cover(d.ncer, d.tile_bytes)
        self.assertEqual(
            an.stored_fs, btchr.derived_footprint_scale(d.n_tiles, len(d.ncer.cells))
        )
        self.assertEqual(an.n_oams, len(an.boxes))
        self.assertTrue(all(0.0 <= b.fill <= 1.0 for b in an.boxes))
        self.assertTrue(all(b.slots >= 1 for b in an.boxes))
        self.assertLessEqual(an.total_slots * an.slot_tiles, an.fs)
        # boxes are tile-aligned and cover every opaque tile of the union
        xo, yo = an.origin
        w, h = an.size
        gc, gr = w // 8, h // 8
        ci = [btchr.render_cell_indexed(c, d.tile_bytes, w, h, xo, yo, d.ncer.boundary_bytes)
              for c in d.ncer.cells]
        union, *_ = ncer.union_tile_mask(ci, [(w, h)] * len(d.ncer.cells), 1)
        covered = set()
        for b in an.boxes:
            for j in range(b.h // 8):
                for k in range(b.w // 8):
                    covered.add((b.tile_col + k, b.tile_row + j))
        uncovered = sum(1 for ty in range(gr) for tx in range(gc)
                        if union[ty][tx] and (tx, ty) not in covered)
        self.assertEqual(uncovered, 0, "map cover must cover every opaque union tile")

    def test_layout_from_rects_footprint(self):
        # Pure: two 16×16 OBJs = 2 slots × 4 tiles = 8 tiles at boundary 256.
        rects = [(0, 0, 2, 2), (2, 0, 2, 2)]
        oams, _plans, _total, fs, n = ncer.layout_from_rects(
            rects, 0, 0, [(32, 16)], slot_tiles=4
        )
        self.assertEqual((n, fs), (2, 8))
        self.assertEqual([(o.w, o.h) for o in oams[0]], [(16, 16), (16, 16)])

    def test_manual_oam_roundtrips_and_guards_coverage(self):
        # Feeding a sprite's own OAM cover back through the manual rebuild is
        # lossless (pixels + position + shared structure) and keeps fs; dropping
        # an OBJ uncovers art and must be refused, not silently dropped.
        entries = _group_entries(self.pak, TOUCANMON)
        d = btchr.decode_digimon(self.pak, TOUCANMON)
        an = btchr.analyze_oam_cover(d.ncer, d.tile_bytes)
        rects = [(b.tile_col, b.tile_row, b.w // 8, b.h // 8) for b in an.boxes]
        unc, _opq = btchrspr.manual_oam_coverage(entries, rects)
        self.assertEqual(unc, 0)
        spr, _old, _new = btchrspr.rebuild_with_manual_oam(entries, rects)
        self.assertEqual(
            _visible_pixels_abs(entries), _visible_pixels_abs(list(spr.entries)),
            "manual re-lay of the current cover must be pixel + position identical",
        )
        self.assertTrue(_cells_share_structure(list(spr.entries)))
        # drop the OBJ holding the most art → uncovered tiles → refuse
        idx = max(range(len(an.boxes)), key=lambda i: an.boxes[i].opaque_tiles)
        fewer = [r for j, r in enumerate(rects) if j != idx]
        self.assertGreater(btchrspr.manual_oam_coverage(entries, fewer)[0], 0)
        with self.assertRaises(ValueError):
            btchrspr.rebuild_with_manual_oam(entries, fewer)

    def test_empty_sprite_declines(self):
        # g400 is an all-transparent placeholder (a 'Gaiomon' sprite_map stub):
        # it re-covers to zero tiles. A 0-footprint sprite is degenerate, so
        # compress must decline it rather than install an empty group.
        entries = _group_entries(self.pak, 400)
        with self.assertRaises(ValueError) as cm:
            btchrspr.compress_existing(entries)
        self.assertIn("empty", str(cm.exception))

    def test_recompression_stays_compressed_and_lossless(self):
        # Re-covering re-centres the OAMs, so the tile-grid alignment can shift
        # the count a little (not a strict fixed point) — but it must stay well
        # under the original and never lose pixels or blow back up.
        entries = _group_entries(self.pak, TOUCANMON)
        spr, old_fs, new_fs = btchrspr.compress_existing(entries)
        spr2, old2, new2 = btchrspr.compress_existing(list(spr.entries))
        self.assertEqual(old2, new_fs, "already-compressed fs is the new baseline")
        self.assertLess(new2, old_fs, "must stay compressed vs the original")
        self.assertEqual(
            _visible_pixels(list(spr.entries)), _visible_pixels(list(spr2.entries)),
            "re-compression must remain lossless",
        )


@unittest.skipUnless(os.path.exists(DUSK_US), "needs Dusk US ROM")
class BatchCompressCommandTests(unittest.TestCase):
    """Compress All OAMs: one undo step applies many re-covers, each rewriting
    its slot's chrsize tpf (the wild-spawn budget currency) and preserving the
    slot's digimon id; undo restores every byte."""

    @classmethod
    def setUpClass(cls):
        from editor.session import RomSession
        cls.session = RomSession.from_file(DUSK_US)

    def test_scan_finds_shrinkers_and_skips_stubs(self):
        # The header-bar 'Compress All' menu calls this pure scan; it must find
        # the bulk of the PAK (incl. the recovered tall sprites) and skip the
        # empty stubs.
        from editor.widgets.btchr_browser import scan_compressible_btchr

        ports, stats = scan_compressible_btchr(self.session)
        gids = {g for g, _ in ports}
        self.assertGreater(len(ports), 250, "expected most sprites to shrink")
        self.assertIn(TOUCANMON, gids)
        self.assertIn(411, gids, "tall Gaiomon should compress now")
        self.assertNotIn(400, gids, "empty stub must be skipped")
        self.assertGreater(stats["old_sum"], stats["new_sum"])
        self.assertEqual(len(ports), len(stats["savers"]))

    def test_scan_cancel_returns_none(self):
        from editor.widgets.btchr_browser import scan_compressible_btchr

        self.assertIsNone(
            scan_compressible_btchr(self.session, should_cancel=lambda: True)
        )

    def test_batch_applies_and_undoes_exactly(self):
        from editor.commands import BatchCompressBtchrCommand

        s = self.session
        p = s.sprite_pak("DAT/BTCHR.PAK")
        targets = [TOUCANMON, 409, 411, 297]  # incl. the recovered tall sprites
        ports, expect, snap = [], {}, {}
        for g in targets:
            ent = [bytes(p.entries[g * 5 + i]) for i in range(5)]
            spr, _old, new = btchrspr.compress_existing(ent)
            ports.append((g, spr))
            expect[g] = new
            snap[g] = (ent, s.current_chrsize_word(g))

        cmd = BatchCompressBtchrCommand(s, ports, "test batch")
        cmd.redo()
        for g in targets:
            word = s.current_chrsize_word(g)
            self.assertEqual((word >> 16) & 0xFFFF, expect[g], f"g{g} tpf")
            self.assertEqual(word & 0xFFFF, snap[g][1] & 0xFFFF, f"g{g} id kept")
            self.assertNotEqual(
                [bytes(p.entries[g * 5 + i]) for i in range(5)], snap[g][0],
                f"g{g} entries should have changed",
            )
        cmd.undo()
        for g in targets:
            self.assertEqual(
                [bytes(p.entries[g * 5 + i]) for i in range(5)], snap[g][0],
                f"g{g} entries restored",
            )
            self.assertEqual(s.current_chrsize_word(g), snap[g][1], f"g{g} chrsize restored")


if __name__ == "__main__":
    unittest.main()
