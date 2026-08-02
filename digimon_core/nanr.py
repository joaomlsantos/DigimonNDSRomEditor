"""NANR (animation resource) parser for ``SPR_ANM.PAK``.

The SPR_* sprite trio (CHR/PAL/CEL) is paired by index with a fourth
parallel pak, ``SPR_ANM.PAK``, holding one NANR per sprite. For most
entries (icons, portraits, single-frame UI) the NANR is a stub with one
sequence of one frame; for **move / effect sprites** it carries real
multi-frame sequences that walk the NCER's cells over time.

Format confirmed empirically against all 1627 vanilla Dusk SPR_ANM
entries (see ``_probe_spr_anm.py``): every one of 6028 animation frames'
cell index resolves inside the matching SPR_CEL's cell count. Layout
(standard Nintendo ``RNAN`` / ``ABNK``):

    RNAN generic header (0x10 B): magic, BOM, version, size, hdr, nblocks
    ABNK block ("KNBA"):
        +0x00 u16 nSequences
        +0x02 u16 nTotalFrames
        +0x04 u32 seqArrayOffset    ] all relative to the block-data
        +0x08 u32 frameArrayOffset  ] start (magic+size, i.e. block+8)
        +0x0C u32 dataArrayOffset   ]
        +0x10 u32 pad ×2
    sequence[i] (16 B): nFrames u16, startFrame u16, type u32,
        mode u32, frameArrayOfs u32
    frame[j]    ( 8 B): dataOfs u32, duration u16, pad u16 (0xBEEF)
    data (index): u16 cellIndex (+ format-dependent trailing bytes)

The ``type`` u32 packs the frame-data format in its low u16 and a
cell-reference flag (always 1 in vanilla) in its high u16. The three
frame-data element formats — cell index is always the first u16 —::

    index     (0):  u16 cell, u16 pad                              (4 B)
    SRT       (1):  u16 cell, u16 rot, s32 sx, s32 sy, s16 px, s16 py (16 B)
    translate (2):  u16 cell, u16 pad, s16 px, s16 py               (8 B)

``rot`` is a 16-bit angle (``0x10000`` == 360°); ``sx``/``sy`` are 20.12
fixed-point scale factors (``0x1000`` == 1.0); ``px``/``py`` are pixel
translations. Confirmed against vanilla: ``0x00c7`` scales (an egg
shrinking 1.0→0.01), ``0x0273`` translates (a slide), ``0x00ad`` rotates.

:func:`parse_nanr` decodes the full transform; :func:`serialize_nanr`
writes it back (emitting one data element per frame — the vanilla files
dedupe shared elements, but a non-deduped layout is equally valid and
lets each frame be edited independently). Reuses
:func:`digimon_core.sprite.find_block` /
:func:`digimon_core.sprite.maybe_decompress` so callers can hand it the
raw (RLE-30 compressed) PAK entry or a decompressed NANR.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List

from .sprite import find_block, maybe_decompress


# Frame-data element formats — the low u16 of the sequence ``type`` field.
ELEMENT_INDEX = 0     # u16 cell index (+ pad)
ELEMENT_SRT = 1       # index + rotation + scale + translation
ELEMENT_INDEX_T = 2   # index + translation

# Byte size of one frame-data element per format.
_ELEMENT_SIZE = {ELEMENT_INDEX: 4, ELEMENT_SRT: 16, ELEMENT_INDEX_T: 8}

# Fixed-point / angle constants for the SRT transform.
SCALE_ONE = 0x1000    # 20.12 fixed-point 1.0
ROT_FULL = 0x10000    # 16-bit angle wrap (== 360°)

# Playback modes — the sequence ``mode`` field. DWDD SPR_ANM only uses
# 1 (play once) and 2 (loop); ping-pong variants exist in the format but
# aren't present in vanilla.
MODE_ONCE = 1
MODE_LOOP = 2


@dataclass
class NanrFrame:
    """One animation frame: show NCER cell ``cell`` for ``duration`` ticks.

    ``duration`` is a 60 Hz vblank count — the same tick unit BTCHR's
    mini-header uses. The affine fields are the raw stored values (see
    module docstring); they default to identity so index-format frames
    carry no transform. Stored raw rather than as floats so a
    parse → serialize round-trip is byte-exact.
    """
    cell: int
    duration: int
    rot: int = 0                  # raw 16-bit angle, ROT_FULL == 360°
    scale_x: int = SCALE_ONE      # 20.12 fixed-point, SCALE_ONE == 1.0
    scale_y: int = SCALE_ONE
    trans_x: int = 0
    trans_y: int = 0

    @property
    def scale_x_f(self) -> float:
        return self.scale_x / SCALE_ONE

    @property
    def scale_y_f(self) -> float:
        return self.scale_y / SCALE_ONE

    @property
    def rotation_deg(self) -> float:
        return (self.rot & 0xFFFF) / ROT_FULL * 360.0

    @property
    def is_identity(self) -> bool:
        return (
            self.rot == 0
            and self.scale_x == SCALE_ONE and self.scale_y == SCALE_ONE
            and self.trans_x == 0 and self.trans_y == 0
        )


@dataclass
class NanrSequence:
    """One named animation track (an ABNK sequence)."""
    frames: List[NanrFrame]
    start_frame: int   # loop-restart frame index within this sequence
    element: int       # ELEMENT_* — frame-data format
    mode: int          # MODE_* — playback mode

    @property
    def loops(self) -> bool:
        return self.mode != MODE_ONCE


@dataclass
class Nanr:
    """Parsed NANR: the list of animation sequences for one sprite."""
    sequences: List[NanrSequence]

    @property
    def has_animation(self) -> bool:
        """True when any sequence advances through more than one frame."""
        return any(len(s.frames) > 1 for s in self.sequences)


def parse_nanr(raw: bytes) -> Nanr:
    """Parse an NANR (compressed or raw). Raises ``ValueError`` on a
    non-NANR payload or a structurally malformed animation bank."""
    raw = maybe_decompress(raw)
    if raw[:4] != b"RNAN":
        raise ValueError(f"not NANR: {raw[:4]!r}")
    try:
        abnk = find_block(raw, b"KNBA")
        base = abnk + 8
        n_seq = struct.unpack_from("<H", raw, base)[0]
        seq_off, frame_off, data_off = struct.unpack_from("<III", raw, base + 4)
        seq_base = base + seq_off
        frame_base = base + frame_off
        data_base = base + data_off

        sequences: List[NanrSequence] = []
        for si in range(n_seq):
            so = seq_base + si * 16
            n_frames, start_frame, type_word, mode, frame_ofs = struct.unpack_from(
                "<HHIII", raw, so
            )
            element = type_word & 0xFFFF
            frames: List[NanrFrame] = []
            fb = frame_base + frame_ofs
            for fi in range(n_frames):
                data_ptr, duration, _pad = struct.unpack_from("<IHH", raw, fb + fi * 8)
                dv = data_base + data_ptr
                if element == ELEMENT_SRT:
                    cell, rot, sx, sy, px, py = struct.unpack_from("<HHiihh", raw, dv)
                    frames.append(NanrFrame(
                        cell=cell, duration=duration, rot=rot,
                        scale_x=sx, scale_y=sy, trans_x=px, trans_y=py,
                    ))
                elif element == ELEMENT_INDEX_T:
                    cell, _pad2, px, py = struct.unpack_from("<HHhh", raw, dv)
                    frames.append(NanrFrame(
                        cell=cell, duration=duration, trans_x=px, trans_y=py,
                    ))
                else:
                    cell = struct.unpack_from("<H", raw, dv)[0]
                    frames.append(NanrFrame(cell=cell, duration=duration))
            sequences.append(NanrSequence(
                frames=frames,
                start_frame=start_frame,
                element=element,
                mode=mode,
            ))
    except struct.error as exc:
        raise ValueError(f"malformed NANR: {exc}") from exc
    return Nanr(sequences)


def serialize_nanr(n: Nanr, template_raw: bytes) -> bytes:
    """Re-encode ``n`` into a decompressed NANR, reusing ``template_raw``'s
    generic header and any post-ABNK blocks (LBAL labels / TXEU) verbatim.

    Only the ABNK animation bank is rebuilt. Each frame is emitted with its
    own data element (no cross-frame dedup), so editing one frame's
    transform never disturbs another. The sequence element format is
    preserved per sequence, so index / SRT / translate sequences stay in
    their original layout. Raises ``ValueError`` on a non-NANR template or
    an unknown element format.
    """
    raw = maybe_decompress(template_raw)
    if raw[:4] != b"RNAN":
        raise ValueError(f"not NANR: {raw[:4]!r}")
    abnk = find_block(raw, b"KNBA")
    abnk_size = struct.unpack_from("<I", raw, abnk + 4)[0]
    trailer = raw[abnk + abnk_size:]  # LBAL + TXEU (+ padding), position-independent

    seq_bytes = bytearray()
    frame_bytes = bytearray()
    data_bytes = bytearray()
    for s in n.sequences:
        if s.element not in _ELEMENT_SIZE:
            raise ValueError(f"unknown element format {s.element}")
        frame_ofs = len(frame_bytes)
        for fr in s.frames:
            data_ptr = len(data_bytes)
            if s.element == ELEMENT_SRT:
                data_bytes += struct.pack(
                    "<HHiihh", fr.cell & 0xFFFF, fr.rot & 0xFFFF,
                    fr.scale_x, fr.scale_y, fr.trans_x, fr.trans_y,
                )
            elif s.element == ELEMENT_INDEX_T:
                data_bytes += struct.pack(
                    "<HHhh", fr.cell & 0xFFFF, 0xBEEF, fr.trans_x, fr.trans_y,
                )
            else:
                data_bytes += struct.pack("<HH", fr.cell & 0xFFFF, 0xBEEF)
            frame_bytes += struct.pack("<IHH", data_ptr, fr.duration & 0xFFFF, 0xBEEF)
        # High u16 of the type word is the cell-reference flag (1 in all
        # vanilla data); low u16 is the element format.
        type_word = (1 << 16) | (s.element & 0xFFFF)
        seq_bytes += struct.pack(
            "<HHIII", len(s.frames) & 0xFFFF, s.start_frame & 0xFFFF,
            type_word, s.mode, frame_ofs,
        )

    seq_off = 0x18  # after the 0x18-byte ABNK header
    frame_off = seq_off + len(seq_bytes)
    data_off = frame_off + len(frame_bytes)
    body = bytearray()
    body += struct.pack("<HH", len(n.sequences), len(frame_bytes) // 8)
    body += struct.pack("<III", seq_off, frame_off, data_off)
    body += struct.pack("<II", 0, 0)  # padding
    body += seq_bytes + frame_bytes + data_bytes
    while len(body) % 4:  # NANR blocks are 4-byte aligned
        body.append(0)
    new_abnk = b"KNBA" + struct.pack("<I", len(body) + 8) + bytes(body)

    out = bytearray(raw[:0x10]) + new_abnk + trailer
    struct.pack_into("<I", out, 8, len(out))  # patch RNAN file size
    return bytes(out)


def flatten_sequence(seq: NanrSequence) -> List[NanrFrame]:
    """Expand a sequence into one :class:`NanrFrame` per output tick.

    The player advances one entry per timer tick and draws the frame it
    names (cell + transform) — looping is the caller's job (wrap modulo
    ``len``). Mirrors :func:`digimon_core.btchr.flatten_anim_track` so both
    browsers share the same fixed-fps playback shape. A zero-duration frame
    contributes no ticks, exactly as the mini-header path treats it.
    """
    out: List[NanrFrame] = []
    for fr in seq.frames:
        out.extend([fr] * fr.duration)
    return out
