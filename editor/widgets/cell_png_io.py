"""Shared cell-mode PNG IO for sprite browsers.

Both BTCHR (battle sprites, 8bpp single-palette) and SPR_* (icons /
portraits / UI, 4bpp banked or 8bpp banked-concatenated) round-trip
sprites through PNGs that lay out one NCER cell per uniform slot
(``max_w × max_h`` over every cell) and let each cell's OAM rectangles
drive the pixel → tile mapping.

The render path composites cells via OAM into an Indexed8 canvas with
the sprite's effective palette as the colour table. The import path
inverts the OAM → tile mapping: each pixel inside an OAM rectangle is
written straight into the matching tile byte, respecting hflip/vflip.
Tiles outside any OAM are preserved verbatim from the source bytes so
editing one cell doesn't accidentally wipe storage other (possibly
unrendered) cells use.

The helpers are bit-depth aware — caller picks ``is_8bpp`` and the
inner read/write loops switch between 32-byte-per-tile (4bpp) and
64-byte-per-tile (8bpp) packing.

Callers handle: file dialogs, PNG metadata embedding, NCGR/NCLR template
rebuild, undo-command dispatch. The helper just produces / consumes a
QImage and returns the new tile bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtGui import QImage, QPainter, qRgba

from digimon_core import btchr, ncer as ncer_mod, sprite

from ._png_palette import build_palette_from_png, nearest_idx_opaque


Rgb = Tuple[int, int, int]
CellRect = Tuple[int, int, int, int]  # (xmin, ymin, w, h)
CellLayout = Tuple[List[CellRect], int, int]  # (rects, max_w, max_h)


class CellPngError(Exception):
    """User-facing error raised by the helper.

    Callers catch and surface ``title`` + ``message`` through QMessageBox
    (or equivalent). Using a single exception type keeps the helper UI-free
    while letting callers translate failures into native dialogs.
    """

    def __init__(self, title: str, message: str):
        super().__init__(f"{title}: {message}")
        self.title = title
        self.message = message


@dataclass
class OamTileConflicts:
    """Result of :func:`detect_oam_tile_conflicts`.

    ``shared_tiles`` is the set of source-tile indices that more than one
    OAM (across every cell of the sprite) writes through during a cells-
    mode round-trip. ``flip_conflicts`` is the subset whose OAMs disagree
    on hflip/vflip — those are unsalvageable for editing since the same
    source pixels would have to satisfy two contradictory orientations.

    ``has_any`` is the cheap binary signal callers act on; the counts
    feed into user-facing dialogs so the message is concrete.
    """

    shared_tiles: int
    flip_conflicts: int

    @property
    def has_any(self) -> bool:
        return self.shared_tiles > 0


def detect_oam_tile_conflicts(
    ncer: ncer_mod.Ncer, *, is_8bpp: bool,
) -> OamTileConflicts:
    """Find source tiles that more than one OAM writes during a cells round-trip.

    The cells-mode import walks each OAM and writes its rectangle of
    pixels straight into the underlying source tiles. When two OAMs
    reference the same tile range, the second write wins — so the
    edited pixels of OAM A leak into OAM B's rendering after import,
    or vice versa. Memory-efficient sprites (icons / portraits often
    do this) hit it; battle sprites usually don't.

    Granularity is whole tiles, matching the import's write granularity
    (a tile gets fully overwritten when any OAM rectangle covers it).
    ``flip_conflicts`` flags the subset where the orientation differs
    — those tiles can't even be edited "consistently" across their
    OAMs.
    """
    bytes_per_tile = 64 if is_8bpp else 32
    boundary_bytes = ncer.boundary_bytes
    seen: dict = {}
    shared_tiles: set = set()
    flip_conflict_tiles: set = set()
    for cell in ncer.cells:
        for oam in cell.oams:
            base = (oam.tile * boundary_bytes) // bytes_per_tile
            for t in range(base, base + oam.n_tiles):
                key = (oam.hflip, oam.vflip)
                prev = seen.get(t)
                if prev is None:
                    seen[t] = key
                else:
                    shared_tiles.add(t)
                    if prev != key:
                        flip_conflict_tiles.add(t)
    return OamTileConflicts(
        shared_tiles=len(shared_tiles),
        flip_conflicts=len(flip_conflict_tiles),
    )


@dataclass
class CellPngContext:
    """Everything the render / import paths need about the current sprite.

    ``palette`` is the *effective* palette — the caller already decided
    whether to concatenate 4bpp banks (BTCHR 8bpp pattern) or to use a
    single 4bpp bank (SPR_* common case). Length is 16 for 4bpp or 256
    for 8bpp.
    """

    ncer: ncer_mod.Ncer
    tile_bytes: bytes
    n_tiles: int
    palette: List[Rgb]
    is_8bpp: bool


# ---- layout -----------------------------------------------------------


def cell_layout(ncer: ncer_mod.Ncer) -> Optional[CellLayout]:
    """Return ``(rects, max_w, max_h)`` or ``None`` for empty/degenerate.

    Centralised because export, preview, and import must agree on the
    slot dimensions and per-cell origins.
    """
    if not ncer.cells:
        return None
    rects: List[CellRect] = []
    max_w = max_h = 0
    for cell in ncer.cells:
        xmin, ymin, xmax, ymax = btchr.cell_bbox(cell)
        w = xmax - xmin
        h = ymax - ymin
        rects.append((xmin, ymin, w, h))
        if w > max_w:
            max_w = w
        if h > max_h:
            max_h = h
    if max_w <= 0 or max_h <= 0:
        return None
    return rects, max_w, max_h


# ---- coverage-gap overlay ---------------------------------------------


_RED_OVERLAY_RGBA = (255, 0, 0, 96)


def _stamp_red_overlay_in_slot(
    img: QImage,
    cell: ncer_mod.Cell,
    *,
    slot_x: int,
    slot_y: int,
    slot_w: int,
    slot_h: int,
    xmin: int,
    ymin: int,
) -> None:
    """Draw the red checkerboard on gap pixels of one slot in ``img``.

    ``img`` must be ARGB32-compatible (callers convert Indexed8 sources
    first). Uses ``qRgba`` scanline writes over the checker pattern
    ``(px + py) & 1 == 0`` so the underlying sprite pixels stay
    visible through the gaps. Semi-transparent (alpha=96) so it reads
    as an overlay rather than a solid fill.

    Mask coord (0, 0) corresponds to slot pixel (0, 0), which is where
    the cell's bbox origin lands — i.e. OAM coord (xmin, ymin). The
    ``slot_x/slot_y`` offset is re-applied when writing pixels into
    ``img``, not baked into the mask origin (doing that shifted every
    non-cell-0 slot's OAM rectangles off the mask, leaving every pixel
    marked uncovered and the whole slot tinted red).
    """
    mask = btchr.oam_coverage_mask(cell, slot_w, slot_h, xmin, ymin)
    if not mask:
        return
    img_w = img.width()
    img_h = img.height()
    r, g, b, a = _RED_OVERLAY_RGBA
    tint = qRgba(r, g, b, a)
    for py in range(slot_h):
        dy = slot_y + py
        if not (0 <= dy < img_h):
            continue
        row = py * slot_w
        for px in range(slot_w):
            if mask[row + px]:
                continue
            if ((px + py) & 1) != 0:
                continue
            dx = slot_x + px
            if not (0 <= dx < img_w):
                continue
            img.setPixel(dx, dy, tint)


def _to_argb32(img: QImage) -> QImage:
    """Return an ARGB32 copy suitable for overlay-stamping.

    Indexed8 sources have to be widened before ``setPixel`` can write
    the semi-transparent tint. Already-RGBA images pass through with
    just a ``convertToFormat`` no-op copy so the caller can safely
    mutate without touching the shared cached source.
    """
    if img.format() not in (
        QImage.Format_ARGB32, QImage.Format_ARGB32_Premultiplied,
    ):
        return img.convertToFormat(QImage.Format_ARGB32)
    return img.copy()


def overlay_red_gaps_composite(
    img: QImage,
    *,
    ncer: ncer_mod.Ncer,
    layout: CellLayout,
    columns: int,
) -> QImage:
    """Return an ARGB32 copy of ``img`` with gap pixels tinted red for
    every cell in the composite grid."""
    rects, max_w, max_h = layout
    n_cells = len(ncer.cells)
    columns = max(1, min(columns, n_cells))
    out = _to_argb32(img)
    for ci, cell in enumerate(ncer.cells):
        col = ci % columns
        row = ci // columns
        xmin, ymin, _, _ = rects[ci]
        _stamp_red_overlay_in_slot(
            out, cell,
            slot_x=col * max_w, slot_y=row * max_h,
            slot_w=max_w, slot_h=max_h,
            xmin=xmin, ymin=ymin,
        )
    return out


def overlay_red_gaps_single_cell(
    img: QImage,
    *,
    ncer: ncer_mod.Ncer,
    layout: CellLayout,
    cell_idx: int,
) -> QImage:
    """Return an ARGB32 copy of a per-cell PNG with gap pixels tinted red."""
    rects, max_w, max_h = layout
    if not (0 <= cell_idx < len(ncer.cells)):
        return _to_argb32(img)
    out = _to_argb32(img)
    xmin, ymin, _, _ = rects[cell_idx]
    _stamp_red_overlay_in_slot(
        out, ncer.cells[cell_idx],
        slot_x=0, slot_y=0,
        slot_w=max_w, slot_h=max_h,
        xmin=xmin, ymin=ymin,
    )
    return out


# ---- canvas + paint ---------------------------------------------------


def make_indexed_canvas(w: int, h: int, palette: List[Rgb]) -> QImage:
    """Indexed8 QImage with ``palette`` as the colour table.

    Slot 0 is forced to alpha=0 so transparent gutter regions render
    correctly when the PNG is opened in image editors. Other slots get
    full alpha. Works for both 16-entry (4bpp) and 256-entry (8bpp)
    palettes — the unused indices above ``len(palette)`` are never set.
    """
    img = QImage(w, h, QImage.Format_Indexed8)
    ctable = [
        qRgba(r, g, b, 0 if pi == 0 else 255)
        for pi, (r, g, b) in enumerate(palette)
    ]
    img.setColorTable(ctable)
    img.fill(0)
    return img


def _tile_geometry(ctx: CellPngContext) -> Tuple[int, int, int]:
    """``(bytes_per_tile, row_stride, boundary_bytes)`` for the current sprite.

    OAM tile fields are slot indices whose unit is ``boundary_bytes``,
    not tiles. The linear NCGR tile index is
    ``(slot * boundary_bytes) // bytes_per_tile`` — evaluate at the call
    site so 2D-8bpp sprites (boundary=32, bytes_per_tile=64: half a tile
    per slot) resolve correctly. A previous ``tile_mult`` scalar clamped
    to 1 here silently mapped OAM slots one-for-one to tile indices,
    which pushed slots past ``n_tiles`` on multi-cell 2D-8bpp BTCHR
    sprites (e.g. group 77) and cropped the bottom half of every cell.
    """
    bytes_per_tile = 64 if ctx.is_8bpp else 32
    row_stride = 8 if ctx.is_8bpp else 4
    return bytes_per_tile, row_stride, ctx.ncer.boundary_bytes


def paint_cell_indexed(
    img: QImage,
    ctx: CellPngContext,
    cell: ncer_mod.Cell,
    slot_x: int,
    slot_y: int,
    xmin: int,
    ymin: int,
) -> None:
    """Composite one cell's OAMs into ``img`` at ``(slot_x, slot_y)``.

    ``xmin/ymin`` is the cell's bbox origin so the cell's content lands
    at the slot's top-left regardless of where the OAMs sit in OAM
    coords. Index 0 is reserved transparent — never written, so any
    transparent gutter behind the cell stays alpha=0.
    """
    bytes_per_tile, row_stride, boundary_bytes = _tile_geometry(ctx)
    tile_bytes = ctx.tile_bytes
    n_tiles = ctx.n_tiles
    img_w = img.width()
    img_h = img.height()
    for o in cell.oams:
        first_tile = (o.tile * boundary_bytes) // bytes_per_tile
        ox = o.x - xmin
        oy = o.y - ymin
        ntw = o.w // 8
        nth = o.h // 8
        for ty in range(nth):
            for tx in range(ntw):
                tile_idx = first_tile + ty * ntw + tx
                if tile_idx >= n_tiles:
                    continue
                tile_off = tile_idx * bytes_per_tile
                for r in range(8):
                    sr = (7 - r) if o.vflip else r
                    src_row = tile_off + sr * row_stride
                    dst_y = slot_y + oy + ty * 8 + r
                    if not (0 <= dst_y < img_h):
                        continue
                    for c in range(8):
                        sc = (7 - c) if o.hflip else c
                        if ctx.is_8bpp:
                            pi = tile_bytes[src_row + sc]
                        else:
                            pi = (tile_bytes[src_row + (sc >> 1)] >> ((sc & 1) * 4)) & 0xF
                        if pi == 0:
                            continue
                        dst_x = slot_x + ox + tx * 8 + c
                        if not (0 <= dst_x < img_w):
                            continue
                        img.setPixel(dst_x, dst_y, pi)


# ---- render -----------------------------------------------------------


def render_cells_qimage(ctx: CellPngContext, columns: int) -> Optional[QImage]:
    """Composite cells side-by-side in a uniform grid.

    Slot size = ``(max_w, max_h)`` from ``cell_layout``. Cell content
    sits at each slot's top-left. ``columns`` is clamped to
    ``[1, n_cells]``.
    """
    layout = cell_layout(ctx.ncer)
    if layout is None:
        return None
    rects, max_w, max_h = layout
    n_cells = len(ctx.ncer.cells)
    columns = max(1, min(columns, n_cells))
    rows = (n_cells + columns - 1) // columns
    img = make_indexed_canvas(max_w * columns, max_h * rows, ctx.palette)
    for ci, cell in enumerate(ctx.ncer.cells):
        col = ci % columns
        row = ci // columns
        xmin, ymin, _, _ = rects[ci]
        paint_cell_indexed(img, ctx, cell, col * max_w, row * max_h, xmin, ymin)
    return img


def render_one_cell_qimage(ctx: CellPngContext, cell_idx: int) -> Optional[QImage]:
    """Single cell at the union slot size.

    Same canvas + ctable as the composite renderer so the formats are
    pixel-compatible — a per-cell PNG could in principle be tiled
    together to reconstruct the composite.
    """
    layout = cell_layout(ctx.ncer)
    if layout is None:
        return None
    rects, max_w, max_h = layout
    if not (0 <= cell_idx < len(ctx.ncer.cells)):
        return None
    img = make_indexed_canvas(max_w, max_h, ctx.palette)
    xmin, ymin, _, _ = rects[cell_idx]
    paint_cell_indexed(img, ctx, ctx.ncer.cells[cell_idx], 0, 0, xmin, ymin)
    return img


def spr_index_footprint(cel_pak, index: int) -> Optional[Tuple[int, int]]:
    """Return the ``(width, height)`` sprite footprint (largest cell OAM bbox)
    for SPR_* entry ``index``, or ``None`` if out of range / unparseable /
    empty. Cheap dimension probe (parses only the CEL/NCER, no pixels)."""
    if not (0 <= index < len(cel_pak.entries)):
        return None
    try:
        ncer_obj = ncer_mod.parse_ncer(
            sprite.maybe_decompress(cel_pak.entries[index])
        )
    except (ValueError, IndexError):
        return None
    if not ncer_obj.cells:
        return None
    return ncer_mod.sprite_bbox(ncer_obj)


def render_spr_index_qimage(
    chr_pak, pal_pak, cel_pak, index: int, *, palette_bank: int = 0,
) -> Optional[QImage]:
    """Composite the SPR_* sprite at ``index`` into one row of cells.

    Standalone convenience for callers that only have a sprite index (e.g.
    the training-pen editor's ``sprite_id`` preview) rather than the full
    sprite-browser state. Mirrors the browser's effective-palette rule
    (concatenate 4bpp banks when the CHR is 8bpp, else use ``palette_bank``).
    Returns ``None`` for an out-of-range index or an entry that fails to
    parse / has no palette or cells.
    """
    if not (0 <= index < len(chr_pak.entries)):
        return None
    try:
        tile_bytes, bit_depth, *_ = sprite.parse_ncgr(
            sprite.maybe_decompress(chr_pak.entries[index])
        )
        palettes, _ = sprite.parse_nclr(
            sprite.maybe_decompress(pal_pak.entries[index])
        )
        ncer_obj = ncer_mod.parse_ncer(
            sprite.maybe_decompress(cel_pak.entries[index])
        )
    except (ValueError, IndexError):
        return None
    if not palettes or not ncer_obj.cells:
        return None
    chr_8bpp = (bit_depth == 4)
    if chr_8bpp and len(palettes[0]) == 16:
        palette = [c for bank in palettes for c in bank]
    else:
        palette = palettes[min(palette_bank, len(palettes) - 1)]
    bytes_per_tile = 64 if chr_8bpp else 32
    n_tiles = len(tile_bytes) // bytes_per_tile
    ctx = CellPngContext(
        ncer=ncer_obj,
        tile_bytes=tile_bytes,
        n_tiles=n_tiles,
        palette=list(palette),
        is_8bpp=chr_8bpp,
    )
    return render_cells_qimage(ctx, len(ncer_obj.cells))


# ---- import (PNG -> tile bytes) ---------------------------------------


def _write_pixel(
    buf: bytearray,
    tile_off: int,
    sr: int,
    sc: int,
    value: int,
    is_8bpp: bool,
    row_stride: int,
) -> None:
    """Write one palette index into the tile buffer at ``(sr, sc)``.

    4bpp packs two pixels per byte (low nibble = even column, high nibble
    = odd column); the read-modify-write here preserves the unedited
    nibble so an OAM that only covers half a byte doesn't corrupt the
    other half.
    """
    if is_8bpp:
        buf[tile_off + sr * row_stride + sc] = value & 0xFF
        return
    byte_off = tile_off + sr * row_stride + (sc >> 1)
    shift = (sc & 1) * 4
    mask = 0x0F << shift
    buf[byte_off] = (buf[byte_off] & ((~mask) & 0xFF)) | ((value & 0x0F) << shift)


def _decode_cell_into(
    src: QImage,
    cell: ncer_mod.Cell,
    new_tiles: bytearray,
    *,
    ctx: CellPngContext,
    slot_x: int,
    slot_y: int,
    xmin: int,
    ymin: int,
    use_indexed: bool,
    palette: List[Rgb],
    bytes_per_tile: int,
    row_stride: int,
    boundary_bytes: int,
) -> None:
    """Walk one cell's OAMs and write decoded pixels into the tile buffer.

    Inverse of ``paint_cell_indexed``. RGBA pixels go through
    ``nearest_idx_opaque`` (slot 0 reserved transparent); Indexed8
    pixels are written verbatim — the caller has guaranteed the PNG's
    PLTE matches the target palette.
    """
    n_tiles = ctx.n_tiles
    src_w = src.width()
    src_h = src.height()
    for o in cell.oams:
        first_tile = (o.tile * boundary_bytes) // bytes_per_tile
        ox = o.x - xmin
        oy = o.y - ymin
        ntw = o.w // 8
        nth = o.h // 8
        for ty in range(nth):
            for tx in range(ntw):
                tile_idx = first_tile + ty * ntw + tx
                if tile_idx >= n_tiles:
                    continue
                tile_off = tile_idx * bytes_per_tile
                for r in range(8):
                    sr = (7 - r) if o.vflip else r
                    dst_y = slot_y + oy + ty * 8 + r
                    if not (0 <= dst_y < src_h):
                        continue
                    for c in range(8):
                        sc = (7 - c) if o.hflip else c
                        dst_x = slot_x + ox + tx * 8 + c
                        if not (0 <= dst_x < src_w):
                            continue
                        if use_indexed:
                            idx = src.pixelIndex(dst_x, dst_y)
                        else:
                            color = src.pixelColor(dst_x, dst_y)
                            if color.alpha() < 128:
                                idx = 0
                            else:
                                idx = nearest_idx_opaque(
                                    color.red(), color.green(),
                                    color.blue(), palette,
                                )
                        _write_pixel(
                            new_tiles, tile_off, sr, sc, idx,
                            ctx.is_8bpp, row_stride,
                        )


def _count_uncovered_in_slot(
    src: QImage,
    cell: ncer_mod.Cell,
    *,
    slot_x: int,
    slot_y: int,
    slot_w: int,
    slot_h: int,
    xmin: int,
    ymin: int,
    use_indexed: bool,
) -> int:
    """Count non-transparent pixels in ``src`` inside the slot rectangle
    that fall outside every OAM in ``cell``.

    ``xmin/ymin`` is the cell's bbox origin (same convention as
    :func:`_decode_cell_into`). Pixels are "non-transparent" if the
    Indexed8 index is > 0 or the RGBA alpha is >= 128 — matches how
    :func:`_decode_cell_into` decides whether a pixel is content.
    Mask origin is ``(xmin, ymin)`` (see
    :func:`_stamp_red_overlay_in_slot` for the same convention).
    """
    mask = btchr.oam_coverage_mask(cell, slot_w, slot_h, xmin, ymin)
    if not mask:
        return 0
    lost = 0
    src_w = src.width()
    src_h = src.height()
    for py in range(slot_h):
        dy = slot_y + py
        if not (0 <= dy < src_h):
            continue
        row = py * slot_w
        for px in range(slot_w):
            if mask[row + px]:
                continue
            dx = slot_x + px
            if not (0 <= dx < src_w):
                continue
            if use_indexed:
                if src.pixelIndex(dx, dy) != 0:
                    lost += 1
            else:
                if src.pixelColor(dx, dy).alpha() >= 128:
                    lost += 1
    return lost


def count_uncovered_content_composite(
    img: QImage,
    *,
    ncer: ncer_mod.Ncer,
    layout: CellLayout,
    columns: int,
) -> int:
    """Composite-import variant: sum uncovered non-transparent pixels
    across all cells laid out in the ``columns × rows`` grid."""
    rects, max_w, max_h = layout
    n_cells = len(ncer.cells)
    columns = max(1, min(columns, n_cells))
    use_indexed = img.format() == QImage.Format_Indexed8
    if not use_indexed:
        img = img.convertToFormat(QImage.Format_RGBA8888)
    total = 0
    for ci, cell in enumerate(ncer.cells):
        col = ci % columns
        row = ci // columns
        xmin, ymin, _, _ = rects[ci]
        total += _count_uncovered_in_slot(
            img, cell,
            slot_x=col * max_w, slot_y=row * max_h,
            slot_w=max_w, slot_h=max_h,
            xmin=xmin, ymin=ymin,
            use_indexed=use_indexed,
        )
    return total


def count_uncovered_content_per_cell(
    imgs: List[QImage],
    *,
    ncer: ncer_mod.Ncer,
    layout: CellLayout,
) -> int:
    """Per-cell-import variant: each PNG is its own ``max_w × max_h`` slot."""
    rects, max_w, max_h = layout
    if len(imgs) != len(ncer.cells):
        return 0
    use_indexed = imgs[0].format() == QImage.Format_Indexed8
    if not use_indexed:
        imgs = [p.convertToFormat(QImage.Format_RGBA8888) for p in imgs]
    total = 0
    for ci, cell in enumerate(ncer.cells):
        xmin, ymin, _, _ = rects[ci]
        total += _count_uncovered_in_slot(
            imgs[ci], cell,
            slot_x=0, slot_y=0,
            slot_w=max_w, slot_h=max_h,
            xmin=xmin, ymin=ymin,
            use_indexed=use_indexed,
        )
    return total


def import_cells_to_tiles(
    img: QImage,
    *,
    ctx: CellPngContext,
    layout: CellLayout,
    columns: int,
    palette: List[Rgb],
) -> bytes:
    """Slice ``img`` into ``columns × rows`` cell slots and rebuild tile bytes.

    Tile bytes start from ``ctx.tile_bytes`` so tiles outside every OAM
    keep their old contents. Overlapping tiles (shared by multiple
    OAMs) are last-write-wins — same as the engine's behaviour where
    shared tiles render identically wherever they're sampled. Raises
    ``CellPngError`` if the image is the wrong size.
    """
    rects, max_w, max_h = layout
    n_cells = len(ctx.ncer.cells)
    columns = max(1, min(columns, n_cells))
    rows = (n_cells + columns - 1) // columns
    expected_w = max_w * columns
    expected_h = max_h * rows
    if img.width() != expected_w or img.height() != expected_h:
        raise CellPngError(
            "Bad image size",
            f"Cells PNG should be {expected_w}×{expected_h} for "
            f"{n_cells} cells in {columns} columns; got "
            f"{img.width()}×{img.height()}.",
        )
    use_indexed = img.format() == QImage.Format_Indexed8
    if not use_indexed:
        img = img.convertToFormat(QImage.Format_RGBA8888)

    new_tiles = bytearray(ctx.tile_bytes)
    bytes_per_tile, row_stride, boundary_bytes = _tile_geometry(ctx)
    for ci, cell in enumerate(ctx.ncer.cells):
        col = ci % columns
        row = ci // columns
        xmin, ymin, _, _ = rects[ci]
        _decode_cell_into(
            img, cell, new_tiles,
            ctx=ctx,
            slot_x=col * max_w, slot_y=row * max_h,
            xmin=xmin, ymin=ymin,
            use_indexed=use_indexed, palette=palette,
            bytes_per_tile=bytes_per_tile, row_stride=row_stride,
            boundary_bytes=boundary_bytes,
        )
    return bytes(new_tiles)


def import_per_cell_to_tiles(
    imgs: List[QImage],
    *,
    ctx: CellPngContext,
    layout: CellLayout,
    palette: List[Rgb],
) -> bytes:
    """Per-cell variant: each cell PNG is its own ``max_w × max_h`` slot.

    All input PNGs must agree on format (Indexed8 vs RGBA) because a
    single NCLR covers every cell — mixed indexed palettes would
    silently produce wrong colours for cells past the first. Raises
    ``CellPngError`` on count, size, or format mismatch.
    """
    rects, max_w, max_h = layout
    n_cells = len(ctx.ncer.cells)
    if len(imgs) != n_cells:
        raise CellPngError(
            "Wrong cell count",
            f"Expected {n_cells} cell PNGs, got {len(imgs)}.",
        )
    for ci, p in enumerate(imgs):
        if p.width() != max_w or p.height() != max_h:
            raise CellPngError(
                "Bad image size",
                f"Cell {ci} PNG is {p.width()}×{p.height()}; "
                f"expected {max_w}×{max_h}.",
            )
    use_indexed = imgs[0].format() == QImage.Format_Indexed8
    for ci, p in enumerate(imgs):
        if (p.format() == QImage.Format_Indexed8) != use_indexed:
            raise CellPngError(
                "Mixed PNG formats",
                f"Cell {ci} PNG format differs from cell 0. Convert all "
                "cells to the same format before importing.",
            )
    if not use_indexed:
        imgs = [p.convertToFormat(QImage.Format_RGBA8888) for p in imgs]

    new_tiles = bytearray(ctx.tile_bytes)
    bytes_per_tile, row_stride, boundary_bytes = _tile_geometry(ctx)
    for ci, cell in enumerate(ctx.ncer.cells):
        xmin, ymin, _, _ = rects[ci]
        _decode_cell_into(
            imgs[ci], cell, new_tiles,
            ctx=ctx,
            slot_x=0, slot_y=0,
            xmin=xmin, ymin=ymin,
            use_indexed=use_indexed, palette=palette,
            bytes_per_tile=bytes_per_tile, row_stride=row_stride,
            boundary_bytes=boundary_bytes,
        )
    return bytes(new_tiles)


# ---- palette rebuild helpers ------------------------------------------


def build_palette_for_per_cell_import(
    imgs: List[QImage], *, total_slots: int, max_w: int, max_h: int,
) -> Optional[List[Rgb]]:
    """Per-cell palette rebuild — PLTE from first cell if indexed,
    otherwise median-cut a strip composite so colours used in any cell
    make it in.

    Returns ``None`` for the fully-transparent RGBA case so the caller
    can surface a user-facing "PNG is fully transparent" error.
    """
    if not imgs:
        return None
    first = imgs[0]
    if first.format() == QImage.Format_Indexed8 and len(first.colorTable()) >= 2:
        return build_palette_from_png(first, total_slots=total_slots)
    strip = QImage(max_w * len(imgs), max_h, QImage.Format_RGBA8888)
    strip.fill(0)
    painter = QPainter(strip)
    for ci, p in enumerate(imgs):
        painter.drawImage(ci * max_w, 0, p)
    painter.end()
    return build_palette_from_png(strip, total_slots=total_slots)
