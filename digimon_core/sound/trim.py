"""SSEQ walker + SBNK/SWAR trim primitives — vendored from the root-level
``sseq_aware_trim.py`` so the editor can audit donor SDATs without depending
on the repo root being on ``sys.path``.

These are pure byte-level functions: no I/O, no CLI, no globals. They mirror
the canonical implementations one-for-one; the root script keeps its own
copy because it predates the editor and ships as a standalone tool. If a
fix lands here, mirror it there (and vice versa) — same fix surface, two
homes.

The full byte-level format documentation lives in
``research_docs/claude_notes/bgm_injection_pipeline.md`` and
``research_docs/claude_notes/sseq_walker_findings.md``.
"""
from __future__ import annotations

import struct
from typing import Dict, List, Set, Tuple


SAMPLED = {1, 2, 3, 5}
DRUM_KIT = 16
KEY_SPLIT = 17


# ---------- SSEQ event walker ---------------------------------------------

def read_vlv(d: bytes, p: int) -> Tuple[int, int]:
    v = 0
    n = 0
    while True:
        b = d[p + n]
        v = (v << 7) | (b & 0x7F)
        n += 1
        if not (b & 0x80):
            return v, n


def walk_events(d, start, progs, opens, visited, base, init_prog=0):
    """Walk one track from ``start``, recording every program that's live
    when a note plays. Default program is 0 until the first 0x81 PROG_CHANGE
    inside this track. All u24 offsets in OPEN_TRACK/JUMP/CALL are relative
    to ``base`` (the SSEQ's data_off)."""
    pos = start
    cur_prog = init_prog
    while pos < len(d):
        if pos in visited:
            return
        visited.add(pos)
        op = d[pos]
        pos += 1
        if op < 0x80:
            progs.add(cur_prog)
            pos += 1
            _, n = read_vlv(d, pos)
            pos += n
        elif op == 0x80:
            _, n = read_vlv(d, pos)
            pos += n
        elif op == 0x81:
            v, n = read_vlv(d, pos)
            pos += n
            cur_prog = v & 0x7F
        elif op == 0x93:
            pos += 1  # track id
            tof = d[pos] | (d[pos + 1] << 8) | (d[pos + 2] << 16)
            pos += 3
            opens.append(base + tof)
        elif op == 0x94:
            tof = d[pos] | (d[pos + 1] << 8) | (d[pos + 2] << 16)
            pos += 3
            tgt = base + tof
            if tgt in visited:
                return
            pos = tgt
        elif op == 0x95:
            tof = d[pos] | (d[pos + 1] << 8) | (d[pos + 2] << 16)
            pos += 3
            walk_events(d, base + tof, progs, opens, visited, base, cur_prog)
        elif 0xB0 <= op <= 0xBF:
            pos += 3
        elif 0xC0 <= op <= 0xDF:
            pos += 1
        elif op in (0xE0, 0xE1, 0xE3):
            pos += 2
        elif op == 0xFC:
            pass
        elif op == 0xFD:
            # RET is a no-op at top level — treating it as END here used to
            # truncate the walker mid-track and dropped real progs.
            pass
        elif op == 0xFF:
            return
        elif op == 0xFE:
            pos += 2
        else:
            return


def sseq_progs(sseq: bytes) -> Set[int]:
    if sseq[0:4] != b"SSEQ" or sseq[0x10:0x14] != b"DATA":
        raise ValueError("not a SSEQ")
    data_off = struct.unpack_from("<I", sseq, 0x18)[0]
    progs: Set[int] = set()
    opens: List[int] = []
    visited: Set[int] = set()
    walk_events(sseq, data_off, progs, opens, visited, data_off)
    while opens:
        tof = opens.pop()
        if tof in visited:
            continue
        walk_events(sseq, tof, progs, opens, visited, data_off)
    return progs


# ---------- SBNK walker ----------------------------------------------------

def sbnk_inst_count(sbnk: bytes) -> Tuple[int, List[int]]:
    explicit = struct.unpack_from("<I", sbnk, 0x38)[0]
    records_start = 0x3C
    body_starts: List[int] = []
    for i in range(explicit):
        rec = records_start + i * 4
        if rec + 4 > len(sbnk):
            break
        t = sbnk[rec]
        body = sbnk[rec + 1] | (sbnk[rec + 2] << 8) | (sbnk[rec + 3] << 16)
        if t != 0 and body > records_start:
            body_starts.append(body)
    return explicit, body_starts


def walk_inst_body(raw: bytes, t: int, body: int) -> List[Tuple[int, int, int]]:
    """Return ``(slot, swav, swav_field_offset_in_file)`` for each ref in
    an instrument body. Slot is the waveArc[] slot, swav is the SWAR-local
    wave index, and the third field lets callers patch the swav in place."""
    refs: List[Tuple[int, int, int]] = []
    if t in SAMPLED:
        if body + 4 > len(raw):
            return refs
        swav = struct.unpack_from("<H", raw, body)[0]
        slot = struct.unpack_from("<H", raw, body + 2)[0]
        refs.append((slot, swav, body))
        return refs
    if t == DRUM_KIT:
        if body + 2 > len(raw):
            return refs
        lo, hi = raw[body], raw[body + 1]
        sub = body + 2
        for _ in range(lo, hi + 1):
            if sub + 12 > len(raw):
                return refs
            st = struct.unpack_from("<H", raw, sub)[0] & 0xFF
            if st in SAMPLED:
                swav = struct.unpack_from("<H", raw, sub + 2)[0]
                slot = struct.unpack_from("<H", raw, sub + 4)[0]
                refs.append((slot, swav, sub + 2))
            sub += 12
        return refs
    if t == KEY_SPLIT:
        if body + 8 > len(raw):
            return refs
        split = raw[body:body + 8]
        n = sum(1 for b in split if b != 0)
        sub = body + 8
        for _ in range(n):
            if sub + 12 > len(raw):
                return refs
            st = struct.unpack_from("<H", raw, sub)[0] & 0xFF
            if st in SAMPLED:
                swav = struct.unpack_from("<H", raw, sub + 2)[0]
                slot = struct.unpack_from("<H", raw, sub + 4)[0]
                refs.append((slot, swav, sub + 2))
            sub += 12
        return refs
    return refs


def sbnk_program_map(sbnk: bytes) -> Tuple[Dict[int, Tuple[int, int, list]], int]:
    """``{prog: (type, body_off, [refs])}`` for every non-empty record."""
    inst_count, _ = sbnk_inst_count(sbnk)
    out: Dict[int, Tuple[int, int, list]] = {}
    for i in range(inst_count):
        rec = 0x3C + i * 4
        t = sbnk[rec]
        body = sbnk[rec + 1] | (sbnk[rec + 2] << 8) | (sbnk[rec + 3] << 16)
        if t == 0:
            continue
        refs = walk_inst_body(sbnk, t, body)
        out[i] = (t, body, refs)
    return out, inst_count


# ---------- SWAR ----------------------------------------------------------

def swar_wave_table(swar: bytes) -> List[Tuple[int, int]]:
    """Return ``[(header_off, header+data_size)]`` per wave."""
    count = struct.unpack_from("<I", swar, 0x38)[0]
    offs = list(struct.unpack_from(f"<{count}I", swar, 0x3c))
    waves: List[Tuple[int, int]] = []
    for i in range(count):
        start = offs[i]
        end = offs[i + 1] if i + 1 < count else len(swar)
        waves.append((start, end - start))
    return waves


def build_trimmed_swar(
    donor_swar: bytes, keep_indices
) -> Tuple[bytes, Dict[int, int]]:
    """Build a new SWAR containing only ``keep_indices`` from ``donor_swar``,
    in order. Returns ``(bytes, old_to_new_index_map)``."""
    waves = swar_wave_table(donor_swar)
    keep_indices = sorted(keep_indices)
    new_count = len(keep_indices)

    header_size = 0x3c + new_count * 4
    wave_bytes: List[bytes] = []
    for old in keep_indices:
        off, sz = waves[old]
        wave_bytes.append(donor_swar[off:off + sz])

    body_off = header_size
    new_offsets: List[int] = []
    payload = bytearray()
    for wb in wave_bytes:
        new_offsets.append(body_off + len(payload))
        payload += wb
    total_size = header_size + len(payload)

    out = bytearray(header_size)
    out[0:4] = b"SWAR"
    struct.pack_into("<HH", out, 4, 0xFEFF, 0x0100)
    struct.pack_into("<I", out, 8, total_size)
    struct.pack_into("<HH", out, 0xc, 0x10, 1)
    out[0x10:0x14] = b"DATA"
    struct.pack_into("<I", out, 0x14, total_size - 0x10)
    struct.pack_into("<I", out, 0x38, new_count)
    for i, no in enumerate(new_offsets):
        struct.pack_into("<I", out, 0x3c + i * 4, no)
    out += payload
    old_to_new = {old: i for i, old in enumerate(keep_indices)}
    return bytes(out), old_to_new
