"""Profile import_layer_from_png on a synthetic high-variety 512x256 PNG.

Goal: identify which step (multi-bank Lloyd, dedup, cluster_tiles_to_max,
build steps) actually consumes the wall-clock time the user perceives as a
near-freeze. Output a per-step breakdown to inform algorithm changes.
"""
from __future__ import annotations

import cProfile
import io
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
from PIL import Image

from digimon_core import fnt, btmap, btmap_import


def make_synthetic_png() -> bytes:
    """High-variety 512x256 PNG — many distinct cells so cluster_tiles_to_max
    actually has work to do. Use real-world art if available; else fall back
    to a deterministic pseudo-random tile pattern."""
    rng = np.random.default_rng(seed=42)
    # 64x32 cells of slightly varied 8x8 patches → many unique tiles.
    cells = np.zeros((32, 64, 8, 8, 3), dtype=np.uint8)
    for cy in range(32):
        for cx in range(64):
            base = rng.integers(0, 256, size=3)
            noise = rng.integers(-30, 30, size=(8, 8, 3))
            patch = np.clip(base + noise, 0, 255).astype(np.uint8)
            cells[cy, cx] = patch
    img = cells.transpose(0, 2, 1, 3, 4).reshape(256, 512, 3)
    buf = io.BytesIO()
    Image.fromarray(img, mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    rom_path = r"C:\Workspace\digimon_stuffs\1420 - Digimon World - Dusk (US)_b.nds"
    raw = Path(rom_path).read_bytes()
    ft = fnt.FileTable.from_rom(raw)
    paths = btmap.BtmapFiles("69")
    original_ncgr = ft.slice(raw, paths.layer_a_ncgr)
    original_nscr = ft.slice(raw, paths.layer_a_nscr)
    original_nclr = ft.slice(raw, paths.layer_a_nclr)
    png_bytes = make_synthetic_png()
    print(f"synthetic PNG: {len(png_bytes)} bytes")

    # Warm import (loads numpy / PIL JITs)
    t0 = time.perf_counter()
    btmap_import.import_layer_from_png(
        png_bytes,
        target_width_px=512, target_height_px=256,
        original_ncgr=original_ncgr,
        original_nscr=original_nscr,
        original_nclr=original_nclr,
        palette_bank=0,
        is_transparent_layer=False,
        max_tiles=1024,
        use_multi_bank=True,
        available_banks=[b for b in range(16) if b != 1],
    )
    t1 = time.perf_counter()
    print(f"warm total: {t1 - t0:.2f}s")

    # Profile a second run
    prof = cProfile.Profile()
    prof.enable()
    result = btmap_import.import_layer_from_png(
        png_bytes,
        target_width_px=512, target_height_px=256,
        original_ncgr=original_ncgr,
        original_nscr=original_nscr,
        original_nclr=original_nclr,
        palette_bank=0,
        is_transparent_layer=False,
        max_tiles=1024,
        use_multi_bank=True,
        available_banks=[b for b in range(16) if b != 1],
    )
    prof.disable()
    print(f"\nstats: {result.stats}")
    print()
    ps = pstats.Stats(prof).sort_stats("cumulative")
    ps.print_stats(25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
