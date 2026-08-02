"""MCHR_ANM.PAK — overworld-sprite animation codec.

The overworld sprite set (MCHR) is four parallel paks of 890 entries:
``MCHR_CHR`` (raw 4bpp frame strips — see :mod:`digimon_core.mchr`),
``MCHR_PAL`` (palettes), ``MCHR_HIT`` (hitboxes), and ``MCHR_ANM`` — the
animation sequences. ``MCHR_ANM`` is a DWDD-custom format (not NANR).

Format (reverse-engineered; byte-faithful round-trip verified across all
890 vanilla Dusk entries)::

    header      10 bytes                      (opaque — preserved verbatim)
    animation*  { frame_record* , SEP }       one run of records per anim
    END                                        entry terminator

    SEP = 7 × u16 0xFFFF   (14 bytes)          separates animations
    END = 7 × u16 0xFFFE   (14 bytes)          ends the entry

Each frame record is 7 × u16 = 14 bytes::

    [p0, p1, p2, p3, flag, frame_index, duration]

``frame_index`` (field 5) indexes the sprite's MCHR_CHR frames and
``duration`` (field 6) is the on-screen tick count — both confirmed
empirically (frame_index is < the sprite's frame count 98.5% of the time
and alternates in walk cycles; duration is constant across a cycle). The
leading ``p0..p3`` and ``flag`` are not yet identified (candidates: x/y
offset + OAM attributes) — this codec preserves them **verbatim** so only
the confirmed fields are ever touched.

An overworld sprite carries ~16–23 animations (presumably facing ×
state). Module surface mirrors :mod:`digimon_core.nanr`:

- :func:`parse_mchr_anm` / :func:`serialize_mchr_anm`
- :func:`flatten_animation`
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Tuple

from .sprite import maybe_decompress


HEADER_SIZE = 10
RECORD_SIZE = 14           # 7 × u16
_SEP = b"\xff" * RECORD_SIZE        # 7 × u16 0xFFFF — animation separator
_END = b"\xfe\xff" * 7              # 7 × u16 0xFFFE — entry terminator


@dataclass
class MchrAnimFrame:
    """One animation frame: show MCHR_CHR frame ``frame`` for ``duration``
    ticks. ``params`` holds the five leading u16s of the record
    (``p0..p3`` + ``flag``) verbatim — semantics unconfirmed, so they're
    carried through untouched on a round-trip."""
    frame: int
    duration: int
    params: Tuple[int, int, int, int, int] = (0, 0, 0, 0, 0)


@dataclass
class MchrAnimation:
    """One animation track — an ordered run of frame records."""
    frames: List[MchrAnimFrame] = field(default_factory=list)


@dataclass
class MchrAnm:
    """Parsed MCHR_ANM entry for one overworld sprite."""
    header: bytes
    animations: List[MchrAnimation]

    @property
    def has_animation(self) -> bool:
        """True when any animation advances through more than one frame."""
        return any(len(a.frames) > 1 for a in self.animations)


def parse_mchr_anm(raw: bytes) -> MchrAnm:
    """Parse an MCHR_ANM entry (compressed or raw). Raises ``ValueError``
    if the payload isn't 14-byte-record aligned after the header."""
    raw = maybe_decompress(raw)
    if len(raw) < HEADER_SIZE:
        raise ValueError(f"MCHR_ANM entry too short: {len(raw)}B")
    header = raw[:HEADER_SIZE]
    animations: List[MchrAnimation] = []
    cur: List[MchrAnimFrame] = []
    off = HEADER_SIZE
    saw_end = False
    while off + RECORD_SIZE <= len(raw):
        chunk = raw[off:off + RECORD_SIZE]
        off += RECORD_SIZE
        if chunk == _SEP:
            animations.append(MchrAnimation(cur))
            cur = []
            continue
        if chunk == _END:
            saw_end = True
            break
        f = struct.unpack("<7H", chunk)
        cur.append(MchrAnimFrame(
            frame=f[5], duration=f[6],
            params=(f[0], f[1], f[2], f[3], f[4]),
        ))
    if not saw_end:
        raise ValueError("MCHR_ANM entry missing 0xFFFE terminator")
    return MchrAnm(header=header, animations=animations)


def serialize_mchr_anm(m: MchrAnm) -> bytes:
    """Re-encode an :class:`MchrAnm` to decompressed bytes. Inverse of
    :func:`parse_mchr_anm`; a parse → serialize round-trip is byte-exact
    for every vanilla entry."""
    out = bytearray(m.header)
    for anim in m.animations:
        for fr in anim.frames:
            p0, p1, p2, p3, flag = fr.params
            out += struct.pack(
                "<7H",
                p0 & 0xFFFF, p1 & 0xFFFF, p2 & 0xFFFF, p3 & 0xFFFF,
                flag & 0xFFFF, fr.frame & 0xFFFF, fr.duration & 0xFFFF,
            )
        out += _SEP
    out += _END
    return bytes(out)


def flatten_animation(anim: MchrAnimation) -> List[MchrAnimFrame]:
    """Expand an animation into one :class:`MchrAnimFrame` per output tick
    (looping is the caller's job). Mirrors
    :func:`digimon_core.nanr.flatten_sequence`."""
    out: List[MchrAnimFrame] = []
    for fr in anim.frames:
        out.extend([fr] * fr.duration)
    return out
