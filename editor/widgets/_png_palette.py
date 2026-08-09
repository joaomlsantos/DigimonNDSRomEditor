"""Shared 'use the PNG as colour source-of-truth' helper for sprite importers.

When the user imports a PNG via a sprite browser, the colours can come from
one of three places:

1. The PNG's embedded PLTE chunk (indexed-8 mode).
2. A fresh median-cut quantization of the PNG's opaque RGB pixels.
3. The existing NCLR — caller doesn't call this helper at all in that case.

This module covers (1) and (2). Each sprite browser handles indexing into
the returned palette + writing back the NCLR using its own format-specific
encoder (`sprite.build_nclr_from_template`, `mchr.encode_palette_bgr555`,
etc.) so the helper stays Qt-aware but format-agnostic.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtGui import QImage

from digimon_core import sprite


Rgb = Tuple[int, int, int]


def build_palette_from_png(
    img: QImage,
    *,
    total_slots: int,
    alpha_threshold: int = 128,
) -> Optional[List[Rgb]]:
    """Return a ``total_slots``-entry palette derived from ``img``.

    - If ``img`` is ``Format_Indexed8`` with a non-trivial colour table
      (≥ 2 entries), the PLTE is used verbatim, padded with ``(0, 0, 0)``
      to ``total_slots``.
    - Otherwise, ``img`` is treated as RGBA: opaque pixels (alpha ≥
      ``alpha_threshold``) are collected and median-cut to
      ``total_slots - 1`` colours, with slot 0 reserved as a transparent
      placeholder ``(0, 0, 0)``.

    Returns ``None`` if the RGBA path finds no opaque pixels (caller
    surfaces a "PNG is fully transparent" error).

    The caller is responsible for re-indexing image pixels against this
    palette. For the RGBA path, opaque pixels should nearest-match
    against slots ``[1:]`` only so the reserved transparent slot doesn't
    silently consume opaque pixels that happen to be near-black.
    """
    if img.format() == QImage.Format_Indexed8 and len(img.colorTable()) >= 2:
        ctable = img.colorTable()
        out: List[Rgb] = []
        for pi in range(total_slots):
            if pi < len(ctable):
                argb = ctable[pi]
                out.append((
                    (argb >> 16) & 0xFF,
                    (argb >> 8) & 0xFF,
                    argb & 0xFF,
                ))
            else:
                out.append((0, 0, 0))
        return out

    # RGBA quantization fallback.
    if img.format() != QImage.Format_RGBA8888:
        img = img.convertToFormat(QImage.Format_RGBA8888)
    w, h = img.width(), img.height()
    opaque_rgb: List[Rgb] = []
    for py in range(h):
        for px in range(w):
            c = img.pixelColor(px, py)
            if c.alpha() >= alpha_threshold:
                opaque_rgb.append((c.red(), c.green(), c.blue()))
    if not opaque_rgb:
        return None
    quantized = sprite.quantize_palette(opaque_rgb, total_slots - 1)
    palette: List[Rgb] = [(0, 0, 0)] + list(quantized)
    while len(palette) < total_slots:
        palette.append((0, 0, 0))
    return palette


def nearest_idx_opaque(
    r: int, g: int, b: int, palette: List[Rgb],
) -> int:
    """Index of the colour in ``palette[1:]`` nearest to ``(r, g, b)``.

    Slot 0 is reserved transparent — opaque pixels must never collapse
    onto it or the engine renders them invisible.
    """
    best_idx = 1
    best_d = 1 << 30
    for k in range(1, len(palette)):
        pr, pg, pb = palette[k]
        dr = r - pr
        dg = g - pg
        db = b - pb
        d = dr * dr + dg * dg + db * db
        if d < best_d:
            best_d = d
            best_idx = k
            if d == 0:
                break
    return best_idx


def _luma(rgb: Rgb) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def intensity_matched_palette(
    own: List[Rgb],
    source: List[Rgb],
    source_counts: Optional[List[int]] = None,
    dominance: float = 0.08,
) -> List[Rgb]:
    """A palette the size of ``own`` where each slot gets the ``source`` colour
    of nearest brightness (luminance) to that slot's original colour.

    Sprites map specific pixels to specific slots tuned to their *own* palette,
    so a raw slot-for-slot copy of a foreign palette scrambles the shading. This
    remaps by luminance instead — a slot that was dark gets a dark source
    colour, a highlight gets a light one — so the borrowed palette reads
    coherently on the sprite. Slot 0 (transparent) is preserved; source slot 0
    is excluded. Handles differing palette sizes (the counts need not match).

    ``source_counts`` (per-slot pixel usage of the *source sprite*) applies a
    gentle bias toward the source's DOMINANT colours: among candidates of
    similar brightness the more-used one wins, so the borrow reads as the
    source's actual scheme instead of latching onto a brightness-coincident
    rare/edge colour. It is only a tie-breaker — every source colour stays a
    candidate, so the sprite's detail colours survive (a hard cutoff instead
    flattens the sprite toward its few main colours). ``dominance`` is the bias
    strength in normalized-luminance units: 0 = pure nearest-brightness, higher
    = stronger lean on the main colours. Without ``source_counts`` it is pure
    nearest-brightness over every source colour."""
    if not source or len(source) < 2:
        return list(own)
    pool = [(i, source[i]) for i in range(1, len(source))]  # skip transparent
    have_counts = source_counts is not None and len(source_counts) >= len(source)
    max_c = max((source_counts[i] for i, _ in pool), default=0) if have_counts else 0
    scored = [
        (_luma(c), c, (source_counts[i] / max_c if (have_counts and max_c) else 0.0))
        for i, c in pool
    ]
    out = list(own)
    for k in range(1, len(own)):
        target = _luma(own[k])
        out[k] = min(
            scored,
            key=lambda t: abs(t[0] - target) / 255.0 - dominance * t[2],
        )[1]
    return out
