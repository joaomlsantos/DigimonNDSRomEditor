"""SBNK + multi-SWAR flatten primitives — vendored from root-level
``sbnk_flatten.py`` so the editor can build single-SWAR swap payloads
without depending on the repo root being on ``sys.path``.

Goal: collapse a donor SBNK that references waveArc slots 0..3 into a
SBNK whose every instrument points at slot 0, paired with a single SWAR
that concatenates the donor's used waves. The flattened pair is drop-in
usable as a BGM swap without ``--extra-swar`` sacrifice slots.

These are pure byte-level functions: no I/O, no CLI, no globals. The
root script keeps its own copy because it predates the editor and ships
as a standalone tool. If a fix lands here, mirror it there.

Full byte-level format docs live in
``research_docs/claude_notes/bgm_injection_pipeline.md``.
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Set, Tuple


INST_PCM, INST_PSG_PULSE, INST_PSG_NOISE, INST_DIRECT_PCM = 1, 2, 3, 5
INST_DRUM_KIT, INST_KEY_SPLIT = 16, 17
SIMPLE_TYPES = (INST_PCM, INST_PSG_PULSE, INST_PSG_NOISE, INST_DIRECT_PCM)
SUB_RECORD_SIZE = 12
SIMPLE_BODY_SIZE = 10


# ---------- SWAR merging --------------------------------------------------

def parse_swar(buf: bytes) -> List[Optional[bytes]]:
    """Return one wave-body bytes blob per slot (``None`` for empty slots).

    Body size is reconstructible from each wave's 12-byte SWAV header:
    ``(loop_offset + non_loop_length) * 4`` bytes of sample data follow.
    """
    if buf[0:4] != b"SWAR":
        raise ValueError(f"not a SWAR (magic={buf[0:4]!r})")
    if buf[0x10:0x14] != b"DATA":
        raise ValueError("no DATA block at 0x10")
    count = struct.unpack_from("<I", buf, 0x38)[0]
    offsets = struct.unpack_from(f"<{count}I", buf, 0x3C)
    waves: List[Optional[bytes]] = []
    for off in offsets:
        if off == 0:
            waves.append(None)
            continue
        loop_off, non_loop = struct.unpack_from("<HI", buf, off + 6)
        body_size = 12 + (loop_off + non_loop) * 4
        waves.append(bytes(buf[off:off + body_size]))
    return waves


def build_swar(all_waves: List[Optional[bytes]]) -> bytes:
    """Pack ``all_waves`` into a SWAR file. Wave bodies are 4-byte aligned."""
    count = len(all_waves)
    out = bytearray()
    out += b"SWAR"
    out += struct.pack("<HH", 0xFEFF, 0x0100)
    out += b"\x00" * 4
    out += struct.pack("<HH", 0x10, 1)
    out += b"DATA"
    out += b"\x00" * 4
    out += b"\x00" * 32
    out += struct.pack("<I", count)
    out += b"\x00" * (count * 4)

    offsets: List[int] = []
    for body in all_waves:
        if body is None:
            offsets.append(0)
            continue
        while len(out) & 3:
            out += b"\x00"
        offsets.append(len(out))
        out += body

    for i, off in enumerate(offsets):
        struct.pack_into("<I", out, 0x3C + i * 4, off)
    struct.pack_into("<I", out, 0x14, len(out) - 0x10)
    struct.pack_into("<I", out, 0x08, len(out))
    return bytes(out)


def merge_swars(
    swar_buffers: List[bytes],
    used_per_slot: Optional[Dict[int, Set[int]]] = None,
) -> Tuple[bytes, Dict[Tuple[int, int], int], List[int], List[int]]:
    """Concatenate (or prune) waves from each input SWAR into one merged SWAR.

    Returns ``(merged_bytes, swav_remap, kept_counts, total_counts)`` where
    ``swav_remap[(old_slot, old_swav)] = new_swav`` is the merged SWAR's
    compact index. ``used_per_slot=None`` keeps every wave; passing a
    ``{slot: set(swavs)}`` mapping prunes to just the listed waves.
    """
    all_waves: List[Optional[bytes]] = []
    remap: Dict[Tuple[int, int], int] = {}
    kept_counts: List[int] = []
    total_counts: List[int] = []
    for slot, buf in enumerate(swar_buffers):
        waves = parse_swar(buf)
        total_counts.append(len(waves))
        if used_per_slot is None:
            keep = list(range(len(waves)))
        else:
            keep = sorted(used_per_slot.get(slot, set()))
            for old_swav in keep:
                if old_swav >= len(waves):
                    raise ValueError(
                        f"SBNK references slot={slot} swav={old_swav}, but "
                        f"that SWAR has only {len(waves)} waves"
                    )
                if waves[old_swav] is None:
                    raise ValueError(
                        f"SBNK references slot={slot} swav={old_swav}, but "
                        "that wave slot is empty in the source SWAR"
                    )
        for old_swav in keep:
            remap[(slot, old_swav)] = len(all_waves)
            all_waves.append(waves[old_swav])
        kept_counts.append(len(keep))
    return build_swar(all_waves), remap, kept_counts, total_counts


# ---------- SBNK rewriting -------------------------------------------------

def parse_sbnk_records(buf: bytes) -> Tuple[List[Tuple[int, int, int]], int]:
    """Return ``(records, body_start)`` where each record is ``(idx, type, body_off)``."""
    count = struct.unpack_from("<I", buf, 0x38)[0]
    records_start = 0x3C
    records: List[Tuple[int, int, int]] = []
    body_starts: List[int] = []
    for i in range(count):
        rec_off = records_start + i * 4
        if rec_off + 4 > len(buf):
            break
        t = buf[rec_off]
        body = buf[rec_off + 1] | (buf[rec_off + 2] << 8) | (buf[rec_off + 3] << 16)
        if t != 0 and body > records_start:
            body_starts.append(body)
        records.append((i, t, body))
    return records, min(body_starts) if body_starts else records_start


def _remap_subinst(
    buf: bytearray, body_off: int, swav_remap: Dict[Tuple[int, int], int]
) -> None:
    """Rewrite ``(swav, swar_slot)`` at ``body_off`` to ``(new_swav, 0)``.

    Pairs not in ``swav_remap`` are left alone — that's the right call for
    PSG instruments whose swav/swar fields are unused junk that we'd
    otherwise misinterpret as PCM refs.
    """
    swav, slot = struct.unpack_from("<HH", buf, body_off)
    key = (slot, swav)
    if key in swav_remap:
        struct.pack_into("<HH", buf, body_off, swav_remap[key], 0)


def collect_used_swavs(sbnk_bytes: bytes) -> Dict[int, Set[int]]:
    """Walk SBNK records and return ``{slot: set(swavs)}`` of every ref."""
    if sbnk_bytes[0:4] != b"SBNK":
        raise ValueError(f"not a SBNK (magic={sbnk_bytes[0:4]!r})")
    records, _ = parse_sbnk_records(sbnk_bytes)
    used: Dict[int, Set[int]] = {slot: set() for slot in range(4)}

    def record_pair(body_off: int) -> None:
        swav, slot = struct.unpack_from("<HH", sbnk_bytes, body_off)
        if slot < 4:
            used[slot].add(swav)

    for _idx, t, body in records:
        if t == 0:
            continue
        if t in SIMPLE_TYPES:
            record_pair(body)
        elif t == INST_DRUM_KIT:
            lo = sbnk_bytes[body]
            hi = sbnk_bytes[body + 1]
            for k in range(hi - lo + 1):
                sub_off = body + 2 + k * SUB_RECORD_SIZE
                if struct.unpack_from("<H", sbnk_bytes, sub_off)[0] == 0:
                    continue
                record_pair(sub_off + 2)
        elif t == INST_KEY_SPLIT:
            # Region count = non-zero bytes in the 8-byte split; bytes past
            # the last region hold envelope data that would scan as bogus
            # sub-records.
            n_regions = sum(1 for b in sbnk_bytes[body:body + 8] if b != 0)
            for k in range(n_regions):
                sub_off = body + 8 + k * SUB_RECORD_SIZE
                if struct.unpack_from("<H", sbnk_bytes, sub_off)[0] == 0:
                    continue
                record_pair(sub_off + 2)
    return used


def flatten_sbnk(
    sbnk_bytes: bytes, swav_remap: Dict[Tuple[int, int], int]
) -> Tuple[bytes, int, Set[int]]:
    """Return a SBNK with every ``(swav, slot)`` rewritten per ``swav_remap``.

    Returns ``(new_sbnk, touched, unknown_types)``. Records pointing at
    instrument types this code doesn't know are left untouched and noted
    in ``unknown_types``.
    """
    if sbnk_bytes[0:4] != b"SBNK":
        raise ValueError(f"not a SBNK (magic={sbnk_bytes[0:4]!r})")
    out = bytearray(sbnk_bytes)
    records, _ = parse_sbnk_records(out)

    touched = 0
    unknown_types: Set[int] = set()
    for _idx, t, body in records:
        if t == 0:
            continue
        if t in SIMPLE_TYPES:
            _remap_subinst(out, body, swav_remap)
            touched += 1
        elif t == INST_DRUM_KIT:
            lo = out[body]
            hi = out[body + 1]
            for k in range(hi - lo + 1):
                sub_off = body + 2 + k * SUB_RECORD_SIZE
                if struct.unpack_from("<H", out, sub_off)[0] == 0:
                    continue
                _remap_subinst(out, sub_off + 2, swav_remap)
            touched += 1
        elif t == INST_KEY_SPLIT:
            n_regions = sum(1 for b in out[body:body + 8] if b != 0)
            for k in range(n_regions):
                sub_off = body + 8 + k * SUB_RECORD_SIZE
                if struct.unpack_from("<H", out, sub_off)[0] == 0:
                    continue
                _remap_subinst(out, sub_off + 2, swav_remap)
            touched += 1
        else:
            unknown_types.add(t)

    return bytes(out), touched, unknown_types
