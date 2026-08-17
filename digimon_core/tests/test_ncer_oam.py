"""Custom-OAM authoring core: encode/generate/plan/set_cell_oams.

These back the "import a fresh-shape sprite" feature — the pieces that let a
caller cover a new image with hardware-legal OAMs, lay the NCGR tiles out in
the matching order, and install the OAM list into an NCER cell. Verified
against the real BTCHR compositor: a built sprite must re-render to its
source pixels, including BTCHR's 2-tile (128-byte) OAM slot stride.
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


def _load_cel_pak():
    rom_bytes = bytes(rom.loadRom(DUSK_US))
    ft = fnt.FileTable.from_rom(rom_bytes)
    start, end = ft.resolve("DAT/SPR_CEL.PAK")
    return pak.PakFile(rom_bytes[start:end])


LEGAL_OBJ = {
    (8, 8), (16, 16), (32, 32), (64, 64),
    (16, 8), (32, 8), (32, 16), (64, 32),
    (8, 16), (8, 32), (16, 32), (32, 64),
}

SIZES = [(8, 8), (16, 16), (64, 64), (32, 48), (96, 96), (40, 24), (24, 40), (56, 72)]


class EncodeOamTests(unittest.TestCase):
    def test_encode_is_inverse_of_parse(self):
        cases = [
            ncer.Oam(x=0, y=0, w=64, h=64, tile=0, is8bpp=True,
                     hflip=False, vflip=False, pal=0, prio=0),
            ncer.Oam(x=-40, y=-20, w=16, h=32, tile=513, is8bpp=False,
                     hflip=True, vflip=True, pal=15, prio=3),
            ncer.Oam(x=255, y=127, w=8, h=8, tile=1023, is8bpp=True,
                     hflip=True, vflip=False, pal=7, prio=1),
        ]
        for o in cases:
            back = ncer._parse_oam(ncer._encode_oam(o), 0)
            for f in ("x", "y", "w", "h", "tile", "is8bpp", "hflip", "vflip", "pal", "prio"):
                self.assertEqual(getattr(back, f), getattr(o, f), f)


class GenerateGridTests(unittest.TestCase):
    def test_legal_exact_cover_and_contiguous(self):
        for w, h in SIZES:
            oams = ncer.generate_oam_grid(w, h)
            grid = [[0] * (w // 8) for _ in range(h // 8)]
            cur = 0
            for o in oams:
                self.assertIn((o.w, o.h), LEGAL_OBJ, (w, h))
                self.assertEqual(o.tile, cur, (w, h))  # slot_tiles=1 → contiguous
                cur += (o.w // 8) * (o.h // 8)
                for yy in range(o.y // 8, (o.y + o.h) // 8):
                    for xx in range(o.x // 8, (o.x + o.w) // 8):
                        grid[yy][xx] += 1
            self.assertTrue(all(c == 1 for row in grid for c in row), (w, h))

    def test_rejects_non_multiple_of_8(self):
        for bad in [(7, 8), (0, 16), (15, 16), (-8, 8)]:
            with self.assertRaises(ValueError):
                ncer.generate_oam_grid(*bad)


class BtchrRoundTripTests(unittest.TestCase):
    """Build a sprite from scratch and confirm the engine-matching compositor
    re-renders it to the exact source image (2-tile slot stride)."""

    def _roundtrip(self, w, h, slot_tiles):
        indexed = bytearray(w * h)
        for y in range(h):
            for x in range(w):
                indexed[y * w + x] = 1 + ((x * 7 + y * 13) % 254)  # non-zero
        oams = ncer.generate_oam_grid(w, h, slot_tiles=slot_tiles)
        plan, total = ncer.oam_grid_tile_plan(oams, slot_tiles=slot_tiles)
        tiles = ncer.encode_indexed_tiles(indexed, w, plan, total, is8bpp=True)
        palette = [(i, i, i) for i in range(256)]
        # 8bpp: boundary_bytes = slot_tiles × 64 (BTCHR ships 128 = 2 tiles).
        rgba, rw, rh = btchr.render_cell_rgba(
            ncer.Cell(oams), tiles, palette, boundary_bytes=64 * slot_tiles,
        )
        self.assertEqual((rw, rh), (w, h))
        for y in range(h):
            for x in range(w):
                po = (y * rw + x) * 4
                self.assertEqual(rgba[po], indexed[y * w + x], (w, h, x, y))
                self.assertEqual(rgba[po + 3], 255)

    def test_btchr_slots(self):
        for w, h in SIZES:
            self._roundtrip(w, h, slot_tiles=2)  # BTCHR: 128-byte / 2-tile slots

    def test_multicell_shared_ncgr(self):
        """Several cells share one concatenated NCGR (BTCHR's 5-cell tile
        bank): no tile-range overlap, and each cell renders to its source."""
        dims = [(16, 16), (32, 16), (24, 24)]
        srcs = []
        for k, (w, h) in enumerate(dims):
            b = bytearray(w * h)
            for y in range(h):
                for x in range(w):
                    b[y * w + x] = 1 + ((x * 5 + y * 11 + k * 37) % 254)
            srcs.append(b)
        per_cell, total, fs = ncer.generate_multicell_oam_grid(dims, slot_tiles=2)
        self.assertEqual(len(per_cell), 3)
        # Uniform chunking (vanilla BTCHR): n_tiles == fs * n_cells and cell i
        # lives in [i*fs, (i+1)*fs).
        self.assertEqual(total, fs * 3)

        tiles = bytearray(total * 64)
        ranges = []
        for ci, (oams, (w, h), s) in enumerate(zip(per_cell, dims, srcs)):
            plan, _ = ncer.oam_grid_tile_plan(oams, slot_tiles=2)
            lo, hi = min(d for d, *_ in plan), max(d for d, *_ in plan)
            ranges.append((lo, hi))
            self.assertGreaterEqual(lo, ci * fs, (ci, lo, fs))
            self.assertLess(hi, (ci + 1) * fs, (ci, hi, fs))
            part = ncer.encode_indexed_tiles(s, w, plan, total, is8bpp=True)
            for d, *_ in plan:
                tiles[d * 64:d * 64 + 64] = part[d * 64:d * 64 + 64]
        # cell tile ranges are disjoint
        for a in range(len(ranges)):
            for b in range(a + 1, len(ranges)):
                self.assertTrue(
                    ranges[a][1] < ranges[b][0] or ranges[b][1] < ranges[a][0],
                    (ranges[a], ranges[b]),
                )
        # each cell renders back to its own source from the shared NCGR
        palette = [(i, i, i) for i in range(256)]
        for oams, (w, h), s in zip(per_cell, dims, srcs):
            rgba, rw, rh = btchr.render_cell_rgba(
                ncer.Cell(oams), bytes(tiles), palette, boundary_bytes=128,
            )
            self.assertEqual((rw, rh), (w, h))
            for y in range(h):
                for x in range(w):
                    self.assertEqual(rgba[(y * rw + x) * 4], s[y * w + x], (w, h, x, y))


class SetCellOamsTests(unittest.TestCase):
    """Exercise set_cell_oams against a real multi-cell ROM NCER."""

    def _find_multicell(self, cel):
        for i in range(cel.count):
            try:
                nc = ncer.parse_ncer(sprite.maybe_decompress(cel.entries[i]))
            except (ValueError, IndexError):
                continue
            if len(nc.cells) >= 2 and all(c.oams for c in nc.cells[:2]):
                return i, sprite.maybe_decompress(cel.entries[i]), nc
        self.skipTest("no multi-cell NCER found")

    def test_replace_preserves_other_cells(self):
        cel = _load_cel_pak()
        _, raw, nc = self._find_multicell(cel)
        others = [[(o.x, o.y, o.w, o.h, o.tile) for o in c.oams] for c in nc.cells]

        custom = [
            ncer.Oam(x=0, y=0, w=32, h=32, tile=0, is8bpp=True,
                     hflip=False, vflip=False, pal=0, prio=0),
            ncer.Oam(x=32, y=0, w=16, h=16, tile=16, is8bpp=True,
                     hflip=True, vflip=False, pal=3, prio=0),
        ]
        out = ncer.set_cell_oams(raw, 0, custom)
        parsed = ncer.parse_ncer(out)

        self.assertEqual(len(parsed.cells), len(nc.cells))
        self.assertEqual(len(parsed.cells[0].oams), 2)
        self.assertEqual((parsed.cells[0].oams[0].w, parsed.cells[0].oams[0].tile), (32, 0))
        self.assertEqual(
            (parsed.cells[0].oams[1].w, parsed.cells[0].oams[1].tile,
             parsed.cells[0].oams[1].hflip, parsed.cells[0].oams[1].pal),
            (16, 16, True, 3),
        )
        # every other cell's OAMs are byte-for-byte preserved
        for ci in range(1, len(nc.cells)):
            got = [(o.x, o.y, o.w, o.h, o.tile) for o in parsed.cells[ci].oams]
            self.assertEqual(got, others[ci], ci)


class BuildKitTests(unittest.TestCase):
    """btchrspr.build_from_cells: assemble a fresh BTCHR sprite from per-cell
    images + palette, then confirm the kit re-decodes to the source pixels."""

    def _load_btchr_pak(self):
        rom_bytes = bytes(rom.loadRom(DUSK_US))
        ft = fnt.FileTable.from_rom(rom_bytes)
        start, end = ft.resolve("DAT/BTCHR.PAK")
        return pak.PakFile(rom_bytes[start:end])

    def test_build_from_cells_roundtrip(self):
        pk = self._load_btchr_pak()
        group = 10
        template = [bytes(pk.entries[group * btchr.GROUP_SIZE + i]) for i in range(5)]
        # Animation frames share a size (the shared-OAM-layout requirement);
        # each cell has distinct pixels so the round-trip is meaningful.
        dims = [(56, 48)] * 5
        cells = [
            bytes(bytearray(
                1 + ((x + y + k * 3) % 15)
                for y in range(h) for x in range(w)
            ))
            for k, (w, h) in enumerate(dims)
        ]
        # collision-free after 5-bit snap: distinct (r, g) on a 16-step grid
        palette = [((i % 16) * 16, (i // 16) * 16, 0) for i in range(256)]

        spr = btchrspr.build_from_cells(cells, dims, palette, template)
        ncgr, nclr, ncer_raw, nanr = (
            sprite.maybe_decompress(spr.entries[i]) for i in (1, 2, 3, 4)
        )
        tiles, _, *_ = sprite.parse_ncgr(ncgr)
        pals, _ = sprite.parse_nclr(nclr)
        nc = ncer.parse_ncer(ncer_raw)
        pal = pals[0] if len(pals[0]) == 256 else [c for bank in pals for c in bank]

        self.assertEqual(len(nc.cells), 5)
        self.assertEqual(len(tiles) // 64, spr.source_tpf * 5)  # n_tiles == fs*n_cells
        self.assertEqual(
            spr.btchrsize_value, len(ncgr) + len(nclr) + len(ncer_raw) + len(nanr),
        )
        inv = {}
        for i in range(1, 16):
            self.assertNotIn(pal[i], inv)  # test palette must be collision-free
            inv[pal[i]] = i
        for c, (w, h), src in zip(nc.cells, dims, cells):
            rgba, rw, rh = btchr.render_cell_rgba(
                c, tiles, pal, boundary_bytes=nc.boundary_bytes,
            )
            self.assertEqual((rw, rh), (w, h))
            for y in range(h):
                for x in range(w):
                    key = tuple(rgba[(y * rw + x) * 4:(y * rw + x) * 4 + 3])
                    self.assertEqual(inv.get(key), src[y * w + x], (w, h, x, y))

    def test_large_sprite_uses_256_boundary_centered(self):
        """A big/tall sprite must switch to the 256-byte OAM slot stride and
        centre its OAMs (signed y field) — and still render exactly."""
        pk = self._load_btchr_pak()
        template = [bytes(pk.entries[10 * btchr.GROUP_SIZE + i]) for i in range(5)]
        dims = [(256, 192)] * 5  # 768 tiles/cell, 192px tall (>127 y range)
        cells = [
            bytes(bytearray(1 + ((x + y + k) % 15) for y in range(h) for x in range(w)))
            for k, (w, h) in enumerate(dims)
        ]
        palette = [((i % 16) * 16, (i // 16) * 16, 0) for i in range(256)]
        spr = btchrspr.build_from_cells(cells, dims, palette, template)
        ncgr, nclr, ncer_raw = (sprite.maybe_decompress(spr.entries[i]) for i in (1, 2, 3))
        tiles, _, *_ = sprite.parse_ncgr(ncgr)
        pals, _ = sprite.parse_nclr(nclr)
        nc = ncer.parse_ncer(ncer_raw)
        pal = pals[0] if len(pals[0]) == 256 else [c for bank in pals for c in bank]

        self.assertEqual(nc.boundary_bytes, 256)  # auto-upgraded stride
        # NCGR RAHC+0x12 must agree with the NCER boundary (0x30 = 256), or
        # the engine fetches tiles at the wrong stride in-game.
        rahc = sprite.find_block(ncgr, b"RAHC")
        self.assertEqual(ncgr[rahc + 0x12], 0x30)
        # centred: OAM y stays in the signed 8-bit field
        for c in nc.cells:
            for o in c.oams:
                self.assertGreaterEqual(o.y, -128)
                self.assertLessEqual(o.y, 127)
        inv = {}
        for i in range(1, 16):
            inv[pal[i]] = i
        for c, (w, h), src in zip(nc.cells, dims, cells):
            rgba, rw, rh = btchr.render_cell_rgba(
                c, tiles, pal, boundary_bytes=nc.boundary_bytes,
            )
            self.assertEqual((rw, rh), (w, h))
            for y in range(0, h, 7):          # sample rows (full scan is slow)
                for x in range(0, w, 5):
                    key = tuple(rgba[(y * rw + x) * 4:(y * rw + x) * 4 + 3])
                    self.assertEqual(inv.get(key), src[y * w + x], (w, h, x, y))

    def test_transparency_aware_covers_only_content(self):
        """A small character on a big transparent field costs tiles for the
        content only — so a sprite that's over-budget as a full rectangle
        (264×200 opaque = 832 tiles/cell) fits once the background is skipped,
        and still renders the content exactly."""
        pk = self._load_btchr_pak()
        template = [bytes(pk.entries[10 * btchr.GROUP_SIZE + i]) for i in range(5)]
        W, H, BW, BH = 264, 200, 80, 96  # 80×96 opaque block in a 264×200 field
        ox, oy = 96, 48
        cells = []
        for k in range(5):
            buf = bytearray(W * H)  # 0 = transparent
            for y in range(BH):
                for x in range(BW):
                    buf[(oy + y) * W + (ox + x)] = 1 + ((x + y + k) % 15)
            cells.append(bytes(buf))
        palette = [((i % 16) * 16, (i // 16) * 16, 0) for i in range(256)]

        spr = btchrspr.build_from_cells(cells, [(W, H)] * 5, palette, template)
        # content is 80×96 = 120 tiles; far below the ~816 full-rect budget
        self.assertLessEqual(spr.source_tpf, 200)
        ncgr, nclr, ncer_raw = (sprite.maybe_decompress(spr.entries[i]) for i in (1, 2, 3))
        tiles, _, *_ = sprite.parse_ncgr(ncgr)
        pals, _ = sprite.parse_nclr(nclr)
        nc = ncer.parse_ncer(ncer_raw)
        pal = pals[0] if len(pals[0]) == 256 else [c for bank in pals for c in bank]
        for c in nc.cells:
            self.assertLessEqual(len(c.oams), 128)
            for o in c.oams:
                self.assertLessEqual(o.tile, 0x3FF)
        inv = {}
        for i in range(1, 16):
            inv[pal[i]] = i
        # each cell's OAM union is exactly the opaque block, and every opaque
        # pixel maps back to a real palette index (nothing corrupted / offset)
        for c in nc.cells:
            rgba, rw, rh = btchr.render_cell_rgba(
                c, tiles, pal, boundary_bytes=nc.boundary_bytes,
            )
            # The OAM union may carry transparent padding (the 50% coverage
            # threshold), but the *opaque* pixels are exactly the block and
            # every one maps to a real palette index (no corruption/offset).
            opaque = sum(
                1 for i in range(rw * rh)
                if rgba[i * 4 + 3] and tuple(rgba[i * 4:i * 4 + 3]) in inv
            )
            self.assertEqual(opaque, BW * BH)

    def test_oversized_sprite_refused(self):
        pk = self._load_btchr_pak()
        template = [bytes(pk.entries[10 * btchr.GROUP_SIZE + i]) for i in range(5)]
        dims = [(264, 200)] * 5  # over the 1024-slot OAM tile ceiling
        cells = [b"\x01" * (w * h) for w, h in dims]
        with self.assertRaises(ValueError):
            btchrspr.build_from_cells(cells, dims, [(0, 0, 0)] * 256, template)

    def test_cell_count_must_match(self):
        pk = self._load_btchr_pak()
        template = [bytes(pk.entries[10 * btchr.GROUP_SIZE + i]) for i in range(5)]
        with self.assertRaises(ValueError):
            btchrspr.build_from_cells(
                [b"\x00" * 64], [(8, 8)],
                [(0, 0, 0)] * 256, template,
            )


class OamDrawOrderTests(unittest.TestCase):
    def _oam(self, tile, prio):
        return ncer.Oam(x=0, y=0, w=8, h=8, tile=tile, is8bpp=False,
                        hflip=False, vflip=False, pal=0, prio=prio)

    def test_equal_priority_paints_lowest_index_last(self):
        # SPR 0x0256 shape: front content (OAM 0-1) precedes its fill (2-5).
        # Back-to-front paint order must end with OAM 0 so it lands on top.
        cell = ncer.Cell([self._oam(t, 0) for t in range(6)])
        order = [o.tile for o in ncer.oams_back_to_front(cell)]
        self.assertEqual(order, [5, 4, 3, 2, 1, 0])

    def test_priority_field_is_the_coarse_key(self):
        # Higher prio number = further back = painted first, regardless of index.
        cell = ncer.Cell([
            self._oam(tile=10, prio=0),   # front
            self._oam(tile=11, prio=3),   # backmost
            self._oam(tile=12, prio=1),
        ])
        order = [o.tile for o in ncer.oams_back_to_front(cell)]
        self.assertEqual(order, [11, 12, 10])


if __name__ == "__main__":
    unittest.main()
