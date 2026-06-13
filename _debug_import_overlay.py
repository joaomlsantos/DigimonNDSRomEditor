"""Diagnostic: import a PNG as a btmap layer and emit two debug artefacts.

Outputs (per source PNG):

  1. ``<stem>_imported.png`` — the post-import result rendered back to RGB
     via the same NCGR/NSCR/NCLR triple that would be written to the ROM.
     Shows what the 1024-tile reduction actually produces.

  2. ``<stem>_overlay.png`` — the original source image scaled up Nx with
     an 8x8 grid + the *final* NSCR tile index drawn on every cell. Cells
     that share a tile index after dedup/clustering share a label, so you
     can eyeball which patches collapsed into the same NCGR slot.

Independent of the editor UI: uses the same ``digimon_core`` import
pipeline so any algorithm change there is reflected here.

Usage:
    python _debug_import_overlay.py SOURCE.png [--map 69] [--layer a]
        [--scale 5] [--out DIR] [--rom PATH]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from digimon_core import btmap, btmap_import, fnt


DEFAULT_ROM = r"C:\Workspace\digimon_stuffs\1420 - Digimon World - Dusk (US)_b.nds"


# ---------------------------------------------------------------------------
# Experimental k-means++ seeded variant of cluster_tiles_to_max.
#
# Only the init step differs from the production version: instead of picking
# the top ``target`` tiles by ref_count, we greedily pick seeds maximising
# ``ref_count[t] * min_dist²(t, already_chosen)``. The Lloyd refinement and
# materialisation steps below are byte-for-byte copies of the production
# code in digimon_core/btmap_import.py — easier to diff one function than
# weave a hook into the original.
# ---------------------------------------------------------------------------


def cluster_tiles_to_max_kpp(
    unique_tiles: Sequence[bytes],
    cell_assignments: Sequence[Tuple[int, int]],
    palette: Sequence[Tuple[int, int, int]],
    *,
    max_tiles: int,
    locked_tile_indices: Optional[Sequence[int]] = None,
    tile_rgb_expanded: Optional[np.ndarray] = None,
    refine_iters: int = 3,
) -> Tuple[List[bytes], List[Tuple[int, int]]]:
    n = len(unique_tiles)
    if n <= max_tiles:
        return list(unique_tiles), list(cell_assignments)

    if tile_rgb_expanded is not None:
        if tile_rgb_expanded.shape != (n, 64 * 3):
            raise ValueError(
                f"tile_rgb_expanded must be shape ({n}, 192), "
                f"got {tile_rgb_expanded.shape}"
            )
        tile_rgb = tile_rgb_expanded.astype(np.float32, copy=False)
    else:
        pal_arr = np.array(palette, dtype=np.float32)
        tile_idx_arr = np.frombuffer(
            b"".join(unique_tiles), dtype=np.uint8,
        ).reshape(n, 64)
        tile_rgb = pal_arr[tile_idx_arr].reshape(n, 64 * 3)

    ref_count = np.zeros(n, dtype=np.float64)
    for t, _ in cell_assignments:
        if 0 <= t < n:
            ref_count[t] += 1.0

    locked = np.zeros(n, dtype=bool)
    if locked_tile_indices is not None:
        for ix in locked_tile_indices:
            if 0 <= ix < n:
                locked[ix] = True

    target = max(max_tiles, int(locked.sum()))
    if target >= n:
        return list(unique_tiles), list(cell_assignments)

    tile_norms = np.einsum("ij,ij->i", tile_rgb, tile_rgb).astype(np.float32)

    # ---- Init: k-means++ seeded, ref_count-weighted ---------------------
    locked_ix = np.where(locked)[0].tolist()
    chosen: List[int] = list(locked_ix)
    remaining = np.ones(n, dtype=bool)
    remaining[chosen] = False

    if chosen:
        seeds_rgb = tile_rgb[chosen]
        seeds_norms = np.einsum("ij,ij->i", seeds_rgb, seeds_rgb)
        dots = tile_rgb @ seeds_rgb.T
        all_d = (
            tile_norms[:, None]
            + seeds_norms[None, :]
            - 2.0 * dots
        )
        min_dist_sq = np.clip(all_d.min(axis=1), 0.0, None).astype(np.float64)
    else:
        bootstrap = int(np.argmax(ref_count))
        chosen.append(bootstrap)
        remaining[bootstrap] = False
        boot_rgb = tile_rgb[bootstrap]
        boot_norm = float(tile_norms[bootstrap])
        min_dist_sq = (
            tile_norms.astype(np.float64)
            + boot_norm
            - 2.0 * (tile_rgb @ boot_rgb).astype(np.float64)
        )
        np.clip(min_dist_sq, 0.0, None, out=min_dist_sq)

    while len(chosen) < target:
        score = ref_count * min_dist_sq
        score[~remaining] = -np.inf
        next_ix = int(np.argmax(score))
        if not np.isfinite(score[next_ix]) or score[next_ix] <= 0.0:
            # Remaining tiles are duplicates of already-chosen ones in
            # RGB-space; fall back to plain ref_count for the rest.
            rc_score = np.where(remaining, ref_count, -np.inf)
            next_ix = int(np.argmax(rc_score))
            if not np.isfinite(rc_score[next_ix]):
                break
        chosen.append(next_ix)
        remaining[next_ix] = False
        new_rgb = tile_rgb[next_ix]
        new_norm = float(tile_norms[next_ix])
        new_dist = (
            tile_norms.astype(np.float64)
            + new_norm
            - 2.0 * (tile_rgb @ new_rgb).astype(np.float64)
        )
        np.minimum(min_dist_sq, new_dist, out=min_dist_sq)

    medoid_arr = np.sort(np.array(chosen, dtype=np.int32))

    # ---- Assign + Lloyd refinement (identical to production) ------------
    def assign_all(medoids: np.ndarray) -> np.ndarray:
        m_rgb = tile_rgb[medoids]
        m_norms = np.einsum("ij,ij->i", m_rgb, m_rgb)
        dots = tile_rgb @ m_rgb.T
        dists = tile_norms[:, None] + m_norms[None, :] - 2.0 * dots
        return dists.argmin(axis=1).astype(np.int32)

    assign = assign_all(medoid_arr)

    for _ in range(max(0, refine_iters)):
        new_medoid_arr = medoid_arr.copy()
        changed = False
        for cluster_ix in range(len(medoid_arr)):
            current = int(medoid_arr[cluster_ix])
            if locked[current]:
                continue
            members = np.where(assign == cluster_ix)[0]
            if len(members) <= 1:
                continue
            member_rgb = tile_rgb[members]
            member_norms = tile_norms[members]
            mem_dots = member_rgb @ member_rgb.T
            pairwise = (
                member_norms[:, None] + member_norms[None, :] - 2.0 * mem_dots
            )
            weights = ref_count[members]
            cost = weights @ pairwise
            best_local = int(cost.argmin())
            best_global = int(members[best_local])
            if best_global != current:
                new_medoid_arr[cluster_ix] = best_global
                changed = True
        if not changed:
            break
        medoid_arr = new_medoid_arr
        assign = assign_all(medoid_arr)

    survivors = np.unique(medoid_arr)
    old_to_new = {int(old): new for new, old in enumerate(survivors.tolist())}
    new_tiles = [unique_tiles[ix] for ix in survivors.tolist()]
    tile_to_medoid = medoid_arr[assign]

    new_assignments: List[Tuple[int, int]] = []
    for t, flip in cell_assignments:
        new_assignments.append((old_to_new[int(tile_to_medoid[t])], flip))

    return new_tiles, new_assignments


def _load_template(rom_path: str, map_id: str, layer: str):
    raw = Path(rom_path).read_bytes()
    ft = fnt.FileTable.from_rom(raw)
    paths = btmap.BtmapFiles(map_id)
    if layer == "a":
        return (
            ft.slice(raw, paths.layer_a_ncgr),
            ft.slice(raw, paths.layer_a_nscr),
            ft.slice(raw, paths.layer_a_nclr),
        )
    elif layer == "b":
        return (
            ft.slice(raw, paths.layer_b_ncgr),
            ft.slice(raw, paths.layer_b_nscr),
            ft.slice(raw, paths.layer_a_nclr),  # palette is shared
        )
    raise ValueError(f"unknown layer {layer!r} (expected 'a' or 'b')")


def _render_imported(result: btmap_import.LayerImportResult) -> Image.Image:
    preview = btmap.render_single_layer(
        result.new_ncgr, result.new_nscr, result.new_nclr,
        backdrop_opaque=True,
    )
    return Image.frombytes(
        "RGBA", (preview.width, preview.height), preview.rgba,
    ).convert("RGB")


def _load_font(cell_px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    target = max(8, int(cell_px * 0.42))
    for name in ("arial.ttf", "DejaVuSans.ttf", "consola.ttf"):
        try:
            return ImageFont.truetype(name, target)
        except OSError:
            continue
    return ImageFont.load_default()


def _build_overlay(
    source_png: Path, result: btmap_import.LayerImportResult, scale: int,
) -> Image.Image:
    src = Image.open(source_png).convert("RGB")
    w, h = src.size
    big = src.resize((w * scale, h * scale), Image.NEAREST)

    _, _, entries = btmap.parse_nscr(result.new_nscr)
    cells_x = w // 8
    cells_y = h // 8
    cell_px = 8 * scale

    overlay = Image.new("RGBA", big.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_font(cell_px)

    grid_color = (0, 0, 0, 110)
    for cy in range(cells_y + 1):
        y = cy * cell_px
        draw.line([(0, y), (big.width, y)], fill=grid_color, width=1)
    for cx in range(cells_x + 1):
        x = cx * cell_px
        draw.line([(x, 0), (x, big.height)], fill=grid_color, width=1)

    for cy in range(cells_y):
        for cx in range(cells_x):
            ix = cy * cells_x + cx
            if ix >= len(entries):
                continue
            entry = entries[ix]
            tile_ix = entry & 0x3FF
            bank = (entry >> 12) & 0xF
            x = cx * cell_px + 2
            y = cy * cell_px + 1
            draw.text(
                (x, y), f"{tile_ix}",
                fill=(255, 255, 255, 255),
                stroke_width=1, stroke_fill=(0, 0, 0, 255),
                font=font,
            )
            draw.text(
                (x, y + cell_px // 2),
                f"b{bank}",
                fill=(255, 220, 90, 230),
                stroke_width=1, stroke_fill=(0, 0, 0, 255),
                font=font,
            )

    return Image.alpha_composite(big.convert("RGBA"), overlay).convert("RGB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path, help="source PNG to import")
    ap.add_argument("--rom", default=DEFAULT_ROM, help="ROM for template files")
    ap.add_argument("--map", default="69", help="btmap id for template")
    ap.add_argument("--layer", default="a", choices=("a", "b"))
    ap.add_argument("--max-tiles", type=int, default=1024)
    ap.add_argument("--scale", type=int, default=5,
                    help="overlay upscale factor (default 5: 8px→40px)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output directory (default: alongside source)")
    ap.add_argument("--no-multi-bank", action="store_true",
                    help="single-bank import (Layer B-style)")
    ap.add_argument("--seeding", choices=("refcount", "kpp", "both"),
                    default="both",
                    help="init seeding for cluster_tiles_to_max "
                         "(default: both — runs each and emits suffixed files)")
    args = ap.parse_args()

    if not args.source.is_file():
        print(f"source not found: {args.source}", file=sys.stderr)
        return 2

    out_dir = args.out or args.source.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.source.stem

    with Image.open(args.source) as im:
        target_w, target_h = im.size

    ncgr_tpl, nscr_tpl, nclr_tpl = _load_template(args.rom, args.map, args.layer)

    is_transparent = (args.layer in ("a", "b"))
    png_bytes = args.source.read_bytes()

    modes = ("refcount", "kpp") if args.seeding == "both" else (args.seeding,)
    original_cluster = btmap_import.cluster_tiles_to_max

    for mode in modes:
        if mode == "kpp":
            btmap_import.cluster_tiles_to_max = cluster_tiles_to_max_kpp
        else:
            btmap_import.cluster_tiles_to_max = original_cluster

        print(f"importing {args.source.name} ({target_w}x{target_h}) "
              f"against map {args.map} layer {args.layer}  [seeding={mode}]...")
        try:
            result = btmap_import.import_layer_from_png(
                png_bytes,
                target_width_px=target_w, target_height_px=target_h,
                original_ncgr=ncgr_tpl,
                original_nscr=nscr_tpl,
                original_nclr=nclr_tpl,
                palette_bank=0,
                is_transparent_layer=is_transparent,
                max_tiles=args.max_tiles,
                use_multi_bank=not args.no_multi_bank,
                available_banks=[b for b in range(16) if b != 1],
            )
        finally:
            btmap_import.cluster_tiles_to_max = original_cluster
        print(f"  stats: {result.stats}")

        suffix = f"_{mode}" if args.seeding == "both" else ""
        imported_path = out_dir / f"{stem}_imported{suffix}.png"
        _render_imported(result).save(imported_path)
        print(f"wrote {imported_path}")

        overlay_path = out_dir / f"{stem}_overlay{suffix}.png"
        _build_overlay(args.source, result, args.scale).save(overlay_path)
        print(f"wrote {overlay_path}  (scale={args.scale}x)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
