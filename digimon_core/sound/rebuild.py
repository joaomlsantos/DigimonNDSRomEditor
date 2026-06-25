"""SDAT block-table + FILE-block rebuild — vendored from root-level
``sdat_swap_bgm.py`` so the editor can produce a new SDAT with swapped
SSEQ/SBNK/SWAR payloads without depending on the repo root being on
``sys.path``.

Pure byte-level functions: no I/O, no CLI. The root script keeps its own
copy because it predates the editor and ships as a standalone tool. If a
fix lands here, mirror it there.

The full byte-level format docs live in
``research_docs/claude_notes/bgm_injection_pipeline.md``.
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple


SDAT_ALIGN = 0x20

# SYMB/INFO record-type ordering — the 8 u32 slots in each block's
# record-offset table, in spec order. Index into both blocks shares this
# table; record 1 (SEQARC) carries nested sub-lists in SYMB.
_SEQ, _SEQARC, _BANK, _WAVE, _PLAYER, _GROUP, _PLAYER2, _STRM = range(8)


def parse_sdat_blocks(data: bytes) -> Tuple[int, Dict[bytes, Tuple[int, int, int]]]:
    """Return ``(total_size, {magic: (block_idx, off, size)})``."""
    if data[0:4] != b"SDAT":
        raise ValueError("not a SDAT container")
    total_size, _, n_blocks = struct.unpack_from("<IHH", data, 8)
    blocks: Dict[bytes, Tuple[int, int, int]] = {}
    for i in range(n_blocks):
        off, sz = struct.unpack_from("<II", data, 0x10 + i * 8)
        if off:
            blocks[bytes(data[off:off + 4])] = (i, off, sz)
    return total_size, blocks


def read_names(data: bytes, symb_off: int, record_rel: int) -> List[str]:
    if record_rel == 0:
        return []
    base = symb_off + record_rel
    count = struct.unpack_from("<I", data, base)[0]
    out: List[str] = []
    for i in range(count):
        sr = struct.unpack_from("<I", data, base + 4 + i * 4)[0]
        if sr == 0:
            out.append("")
            continue
        a = symb_off + sr
        out.append(bytes(data[a:data.find(b"\x00", a)]).decode("ascii", "replace"))
    return out


def info_entry_offsets(data: bytes, info_off: int, record_rel: int) -> List[int]:
    base = info_off + record_rel
    count = struct.unpack_from("<I", data, base)[0]
    offs = struct.unpack_from(f"<{count}I", data, base + 4)
    return [info_off + o if o else 0 for o in offs]


def resolve_sdat_file_id(
    data: bytes, names: List[str], info_entries: List[int], target: str
) -> int:
    """Resolve a SDAT symbol name to its FAT file id. Returns -1 on miss."""
    for i, n in enumerate(names):
        if n == target:
            return struct.unpack_from("<H", data, info_entries[i])[0]
    return -1


def resolve_info_index(names: List[str], target: str) -> int:
    """Return the INFO record-list index for a given symbol name.

    BANK INFO entries store waveArc[4] as INFO record indices (not FAT file
    IDs), so before rewriting them we need to translate "WAVE_BGM99" into
    "the 99th-or-whatever entry in the WAVE INFO record list".
    """
    for i, n in enumerate(names):
        if n == target:
            return i
    return -1


def rebuild_sdat(sdat: bytes, replacements: Dict[int, bytes]) -> bytes:
    """Return new SDAT bytes with ``replacements`` (file_id -> new bytes) applied.

    Re-lays only the FILE block: SYMB/INFO/FAT headers pass through, so any
    INFO-block edits made on the input SDAT (e.g. waveArc patching) survive
    the rebuild. FAT entries are repacked with new offsets/sizes and FILE
    payloads are re-aligned to ``SDAT_ALIGN`` (0x20).
    """
    data = bytearray(sdat)
    _, blocks = parse_sdat_blocks(data)
    _, fat_off, _ = blocks[b"FAT "]
    file_idx, file_off, _ = blocks[b"FILE"]

    fat_count = struct.unpack_from("<I", data, fat_off + 8)[0]
    fat: List[Tuple[int, int, int, int]] = []
    for i in range(fat_count):
        o, s, mem, rsv = struct.unpack_from("<IIII", data, fat_off + 12 + i * 16)
        fat.append((o, s, mem, rsv))
    payloads = [bytes(data[o:o + s]) for (o, s, _, _) in fat]

    for fid, new_bytes in replacements.items():
        payloads[fid] = new_bytes

    file_payload_base = fat[0][0]
    new_file_data = bytearray()
    new_fat: List[Tuple[int, int]] = []
    cursor = file_payload_base
    for p in payloads:
        pad = (-cursor) % SDAT_ALIGN
        if pad:
            new_file_data.extend(b"\x00" * pad)
            cursor += pad
        new_fat.append((cursor, len(p)))
        new_file_data.extend(p)
        cursor += len(p)
    tail_pad = (-cursor) % SDAT_ALIGN
    if tail_pad:
        new_file_data.extend(b"\x00" * tail_pad)
        cursor += tail_pad

    new_file_block_size = cursor - file_off
    new_total_size = cursor

    for i, ((o, s), (_, _, mem, rsv)) in enumerate(zip(new_fat, fat)):
        struct.pack_into("<IIII", data, fat_off + 12 + i * 16, o, s, mem, rsv)
    struct.pack_into("<I", data, file_off + 4, new_file_block_size)
    struct.pack_into("<I", data, file_off + 8, len(payloads))
    struct.pack_into("<I", data, 0x10 + file_idx * 8 + 4, new_file_block_size)
    struct.pack_into("<I", data, 8, new_total_size)

    out = bytearray(data[:file_payload_base])
    out.extend(new_file_data)
    return bytes(out)


# ---- Add-As-New-Entry --------------------------------------------------
#
# ``rebuild_sdat`` above only swaps existing file IDs; ``add_bgm_to_sdat``
# below grows the SDAT instead. Adding a BGM means three new FAT entries
# (SSEQ + SBNK + SWAR), three new INFO records (SEQ/BANK/WAVE), and three
# new SYMB names — every block grows, so the offset bookkeeping is full
# re-serialization, not in-place patching.


def _parse_symb_names(
    data: bytes, symb_off: int, rec_rel: int
) -> List[Optional[str]]:
    """Return a flat name list for one SYMB record (None where slot empty)."""
    if rec_rel == 0:
        return []
    base = symb_off + rec_rel
    count = struct.unpack_from("<I", data, base)[0]
    out: List[Optional[str]] = []
    for i in range(count):
        sr = struct.unpack_from("<I", data, base + 4 + i * 4)[0]
        if sr == 0:
            out.append(None)
            continue
        a = symb_off + sr
        out.append(bytes(data[a:data.find(b"\x00", a)]).decode("ascii", "replace"))
    return out


def _parse_symb_seqarc(
    data: bytes, symb_off: int, rec_rel: int
) -> List[Tuple[Optional[str], List[Optional[str]]]]:
    """Parse the nested SEQARC record: list of (arc_name, [sub_name, ...])."""
    if rec_rel == 0:
        return []
    base = symb_off + rec_rel
    count = struct.unpack_from("<I", data, base)[0]
    out: List[Tuple[Optional[str], List[Optional[str]]]] = []
    for i in range(count):
        name_off, sub_off = struct.unpack_from("<II", data, base + 4 + i * 8)
        if name_off == 0:
            name: Optional[str] = None
        else:
            a = symb_off + name_off
            name = bytes(data[a:data.find(b"\x00", a)]).decode("ascii", "replace")
        sub_names: List[Optional[str]] = []
        if sub_off:
            sb = symb_off + sub_off
            sc = struct.unpack_from("<I", data, sb)[0]
            for j in range(sc):
                sr = struct.unpack_from("<I", data, sb + 4 + j * 4)[0]
                if sr == 0:
                    sub_names.append(None)
                else:
                    a = symb_off + sr
                    sub_names.append(
                        bytes(data[a:data.find(b"\x00", a)]).decode("ascii", "replace")
                    )
        out.append((name, sub_names))
    return out


def _parse_info_bodies(
    data: bytes, info_off: int, info_block_end: int,
    rec_rel: int, next_rec_starts: List[int],
) -> List[Optional[bytes]]:
    """Parse an INFO record list as a list of opaque body byte-blobs.

    Body length is inferred from offset deltas: each non-zero entry's
    body runs to the next non-zero entry's offset (or to the next record
    list, whichever comes first). This is robust to record types we
    don't model explicitly (PLAYER/GROUP/STRM/etc).
    """
    if rec_rel == 0:
        return []
    base = info_off + rec_rel
    count = struct.unpack_from("<I", data, base)[0]
    rel_offs = list(struct.unpack_from(f"<{count}I", data, base + 4))

    bounds = sorted({o for o in rel_offs if o})
    bounds.append(_min_above(next_rec_starts, max(rel_offs) if rel_offs else 0,
                             default=info_block_end - info_off))

    out: List[Optional[bytes]] = []
    for ro in rel_offs:
        if ro == 0:
            out.append(None)
            continue
        nxt = next((b for b in bounds if b > ro), info_block_end - info_off)
        out.append(bytes(data[info_off + ro:info_off + nxt]))
    return out


def _min_above(values: List[int], threshold: int, default: int) -> int:
    above = [v for v in values if v > threshold]
    return min(above) if above else default


def add_bgm_to_sdat(
    sdat: bytes,
    sseq_bytes: bytes,
    sbnk_bytes: bytes,
    swar_bytes: bytes,
    seq_name: Optional[str] = None,
) -> Tuple[bytes, int]:
    """Append a new SSEQ/SBNK/SWAR triple to ``sdat``; return ``(bytes, new_idx)``.

    The new SSEQ lands at SEQ INFO/SYMB array index ``new_idx`` (one past
    the current SEQ count — e.g. 35 for vanilla DWDD), and the same idea
    for the bank/wave arrays. Three new FAT file IDs are allocated and
    their payloads appended to the FILE block with 0x20 alignment.

    ``seq_name`` defaults to ``f"bgm{new_idx:02d}"``; bank/wave names are
    derived as ``BANK_BGM{new_idx:02d}`` / ``WAVE_BGM{new_idx:02d}`` so the
    SDAT-splice path can resolve them by name on save the same way it
    resolves vanilla bgmNN slots.

    Bank waveArc points only at the new WAVE slot (slot 0); other slots
    are -1, matching the single-SWAR convention every built ``BgmSwap``
    already follows.
    """
    if sdat[:4] != b"SDAT":
        raise ValueError("not a SDAT container")

    total_size, blocks = parse_sdat_blocks(sdat)
    if b"INFO" not in blocks or b"FAT " not in blocks or b"FILE" not in blocks:
        raise ValueError("SDAT missing required INFO/FAT/FILE block")
    has_symb = b"SYMB" in blocks
    if not has_symb:
        raise ValueError("add_bgm_to_sdat: SDAT has no SYMB block; cannot add named BGM")

    symb_idx, symb_off, _ = blocks[b"SYMB"]
    info_idx, info_off, info_sz = blocks[b"INFO"]
    fat_idx, fat_off, _ = blocks[b"FAT "]
    file_idx, file_off, _ = blocks[b"FILE"]

    symb_recs = struct.unpack_from("<8I", sdat, symb_off + 8)
    info_recs = struct.unpack_from("<8I", sdat, info_off + 8)

    symb_seq = _parse_symb_names(sdat, symb_off, symb_recs[_SEQ])
    symb_seqarc = _parse_symb_seqarc(sdat, symb_off, symb_recs[_SEQARC])
    symb_bank = _parse_symb_names(sdat, symb_off, symb_recs[_BANK])
    symb_wave = _parse_symb_names(sdat, symb_off, symb_recs[_WAVE])
    symb_player = _parse_symb_names(sdat, symb_off, symb_recs[_PLAYER])
    symb_group = _parse_symb_names(sdat, symb_off, symb_recs[_GROUP])
    symb_player2 = _parse_symb_names(sdat, symb_off, symb_recs[_PLAYER2])
    symb_strm = _parse_symb_names(sdat, symb_off, symb_recs[_STRM])

    info_block_end = info_off + info_sz
    nonzero_recs = sorted(r for r in info_recs if r)
    info_seq = _parse_info_bodies(sdat, info_off, info_block_end, info_recs[_SEQ], nonzero_recs)
    info_seqarc = _parse_info_bodies(sdat, info_off, info_block_end, info_recs[_SEQARC], nonzero_recs)
    info_bank = _parse_info_bodies(sdat, info_off, info_block_end, info_recs[_BANK], nonzero_recs)
    info_wave = _parse_info_bodies(sdat, info_off, info_block_end, info_recs[_WAVE], nonzero_recs)
    info_player = _parse_info_bodies(sdat, info_off, info_block_end, info_recs[_PLAYER], nonzero_recs)
    info_group = _parse_info_bodies(sdat, info_off, info_block_end, info_recs[_GROUP], nonzero_recs)
    info_player2 = _parse_info_bodies(sdat, info_off, info_block_end, info_recs[_PLAYER2], nonzero_recs)
    info_strm = _parse_info_bodies(sdat, info_off, info_block_end, info_recs[_STRM], nonzero_recs)

    fat_count = struct.unpack_from("<I", sdat, fat_off + 8)[0]
    fat_entries: List[Tuple[int, int, int, int]] = []
    payloads: List[bytes] = []
    for i in range(fat_count):
        o, s, mem, rsv = struct.unpack_from("<IIII", sdat, fat_off + 12 + i * 16)
        fat_entries.append((o, s, mem, rsv))
        payloads.append(bytes(sdat[o:o + s]))

    new_seq_idx = len(symb_seq)
    new_bank_idx = len(symb_bank)
    new_wave_idx = len(symb_wave)
    new_seq_fid = len(payloads)
    new_bank_fid = new_seq_fid + 1
    new_wave_fid = new_seq_fid + 2

    if seq_name is None:
        seq_name = f"bgm{new_seq_idx:02d}"
    bank_name = f"BANK_BGM{new_seq_idx:02d}"
    wave_name = f"WAVE_BGM{new_seq_idx:02d}"

    symb_seq.append(seq_name)
    symb_bank.append(bank_name)
    symb_wave.append(wave_name)

    # SEQ INFO body: u16 fid, u16 reserved, u16 bank_idx, u8 vol=127,
    # u8 chnPrio=64, u8 plyPrio=64, u8 player=0, u16 reserved=0
    seq_body = struct.pack(
        "<HHHBBBBH",
        new_seq_fid, 0, new_bank_idx, 127, 64, 64, 0, 0,
    )
    # BANK INFO body: u16 fid, u16 reserved, 4 * s16 waveArc (slot 0 = new wave, 1..3 = -1)
    bank_body = struct.pack(
        "<HHhhhh",
        new_bank_fid, 0, new_wave_idx, -1, -1, -1,
    )
    # WAVE INFO body: u16 fid, u16 reserved
    wave_body = struct.pack("<HH", new_wave_fid, 0)

    info_seq.append(seq_body)
    info_bank.append(bank_body)
    info_wave.append(wave_body)

    fat_entries.append((0, 0, 0, 0))
    fat_entries.append((0, 0, 0, 0))
    fat_entries.append((0, 0, 0, 0))
    payloads.append(sseq_bytes)
    payloads.append(sbnk_bytes)
    payloads.append(swar_bytes)

    new_sdat = _serialize_sdat(
        symb_lists=(symb_seq, symb_seqarc, symb_bank, symb_wave,
                    symb_player, symb_group, symb_player2, symb_strm),
        info_lists=(info_seq, info_seqarc, info_bank, info_wave,
                    info_player, info_group, info_player2, info_strm),
        fat_entries=fat_entries,
        payloads=payloads,
    )
    return new_sdat, new_seq_idx


def _serialize_sdat(
    *,
    symb_lists: Tuple,
    info_lists: Tuple,
    fat_entries: List[Tuple[int, int, int, int]],
    payloads: List[bytes],
) -> bytes:
    """Lay out a full SDAT from parsed components. Always emits all 4 blocks."""
    symb_block = _build_symb_block(symb_lists)
    info_block = _build_info_block(info_lists)
    fat_block = _build_fat_block_header(fat_entries)

    # Block layout: header (0x40) + SYMB + INFO + FAT + FILE
    # Each block 4-byte aligned (matches vanilla DWDD).
    header_size = 0x40
    cursor = header_size
    symb_off = cursor
    cursor += _align4(len(symb_block))
    info_off = cursor
    cursor += _align4(len(info_block))
    fat_off = cursor
    cursor += _align4(len(fat_block))
    file_off = cursor

    # FILE block: magic(4) + size(4) + count(4) + payloads (each 0x20-aligned).
    # First payload base sits at file_off + 0xc to match vanilla layout
    # (and what rebuild_sdat does in-place).
    file_header_size = 0xc
    file_payload_base = file_off + file_header_size

    file_payload_data = bytearray()
    cursor = file_payload_base
    for i, p in enumerate(payloads):
        pad = (-cursor) % SDAT_ALIGN
        if pad:
            file_payload_data.extend(b"\x00" * pad)
            cursor += pad
        off, sz, mem, rsv = fat_entries[i]
        fat_entries[i] = (cursor, len(p), mem, rsv)
        file_payload_data.extend(p)
        cursor += len(p)
    tail_pad = (-cursor) % SDAT_ALIGN
    if tail_pad:
        file_payload_data.extend(b"\x00" * tail_pad)
        cursor += tail_pad

    total_size = cursor
    file_block_size = total_size - file_off

    # Rebuild FAT block with patched offsets
    fat_block = _build_fat_block_header(fat_entries)

    # Build FILE block header
    file_block_header = bytearray(file_header_size)
    file_block_header[0:4] = b"FILE"
    struct.pack_into("<I", file_block_header, 4, file_block_size)
    struct.pack_into("<I", file_block_header, 8, len(payloads))

    # Header
    header = bytearray(header_size)
    header[0:4] = b"SDAT"
    struct.pack_into("<HH", header, 4, 0xFEFF, 0x0100)
    struct.pack_into("<I", header, 8, total_size)
    struct.pack_into("<HH", header, 12, header_size, 4)
    struct.pack_into("<II", header, 0x10, symb_off, len(symb_block))
    struct.pack_into("<II", header, 0x18, info_off, len(info_block))
    struct.pack_into("<II", header, 0x20, fat_off, len(fat_block))
    struct.pack_into("<II", header, 0x28, file_off, file_block_size)

    out = bytearray()
    out.extend(header)
    out.extend(symb_block)
    out.extend(b"\x00" * (_align4(len(symb_block)) - len(symb_block)))
    out.extend(info_block)
    out.extend(b"\x00" * (_align4(len(info_block)) - len(info_block)))
    out.extend(fat_block)
    out.extend(b"\x00" * (_align4(len(fat_block)) - len(fat_block)))
    out.extend(file_block_header)
    out.extend(file_payload_data)

    assert len(out) == total_size, f"serialize mismatch: {len(out)} vs {total_size}"
    return bytes(out)


def _align4(x: int) -> int:
    return (x + 3) & ~3


def _build_symb_block(symb_lists: Tuple) -> bytes:
    """Serialize SYMB. Layout: header + 8 record arrays + seqarc sub-lists + strings."""
    (seq, seqarc, bank, wave, player, group, player2, strm) = symb_lists

    header_size = 0x40  # magic + size + 8*u32 + 0x18 padding
    cursor = header_size

    # Place the 8 record-array headers; record_offsets[i] holds the rel
    # offset to record i's count word, or 0 if the list is empty.
    record_offsets = [0] * 8
    flat_lists = [seq, None, bank, wave, player, group, player2, strm]
    record_areas: List[Optional[Tuple[int, int]]] = [None] * 8  # (count, list_rel_off)

    # Plan record area sizes
    seqarc_main_size = 0 if not seqarc else 4 + 8 * len(seqarc)
    seqarc_sub_sizes = [
        (4 + 4 * len(sub_names)) for _name, sub_names in seqarc
    ] if seqarc else []
    flat_sizes = [
        (4 + 4 * len(lst)) if lst else 0
        for lst in flat_lists
    ]

    # Lay out record-array headers in slot order (0..7)
    for i in range(8):
        if i == _SEQARC:
            if seqarc:
                record_offsets[i] = cursor
                cursor += seqarc_main_size
        else:
            lst = flat_lists[i]
            if lst:
                record_offsets[i] = cursor
                cursor += flat_sizes[i]

    # Then seqarc sub-lists, contiguously after the record arrays
    seqarc_sub_offs: List[int] = []
    for sz in seqarc_sub_sizes:
        seqarc_sub_offs.append(cursor)
        cursor += sz

    # Then string area: append every non-None name; reuse offsets for
    # duplicates so the same string isn't emitted twice.
    string_cache: Dict[str, int] = {}
    string_data = bytearray()
    string_base = cursor

    def intern_str(s: Optional[str]) -> int:
        if s is None:
            return 0
        if s in string_cache:
            return string_cache[s]
        rel = string_base + len(string_data)
        string_cache[s] = rel
        string_data.extend(s.encode("ascii", "replace"))
        string_data.append(0)
        return rel

    # Build flat name offsets
    flat_name_rels: List[List[int]] = [[] for _ in range(8)]
    for i in range(8):
        if i == _SEQARC:
            continue
        lst = flat_lists[i]
        if lst:
            flat_name_rels[i] = [intern_str(s) for s in lst]

    # Build seqarc name + sub-name offsets
    seqarc_name_rels: List[int] = []
    seqarc_sub_name_rels: List[List[int]] = []
    if seqarc:
        for name, sub_names in seqarc:
            seqarc_name_rels.append(intern_str(name))
            seqarc_sub_name_rels.append([intern_str(s) for s in sub_names])

    block_size = string_base + len(string_data)
    out = bytearray(block_size)
    out[0:4] = b"SYMB"
    struct.pack_into("<I", out, 4, block_size)
    for i in range(8):
        struct.pack_into("<I", out, 8 + i * 4, record_offsets[i])
    # 0x18 bytes reserved at 0x28..0x40 stay zero

    # Write flat record arrays
    for i in range(8):
        if i == _SEQARC:
            continue
        ro = record_offsets[i]
        if ro == 0:
            continue
        lst = flat_lists[i]
        struct.pack_into("<I", out, ro, len(lst))
        for j, name_rel in enumerate(flat_name_rels[i]):
            struct.pack_into("<I", out, ro + 4 + j * 4, name_rel)

    # Write seqarc main array + sub-lists
    if seqarc:
        ro = record_offsets[_SEQARC]
        struct.pack_into("<I", out, ro, len(seqarc))
        for j in range(len(seqarc)):
            struct.pack_into(
                "<II", out, ro + 4 + j * 8,
                seqarc_name_rels[j], seqarc_sub_offs[j],
            )
        for j, (_name, sub_names) in enumerate(seqarc):
            sb = seqarc_sub_offs[j]
            struct.pack_into("<I", out, sb, len(sub_names))
            for k, sr in enumerate(seqarc_sub_name_rels[j]):
                struct.pack_into("<I", out, sb + 4 + k * 4, sr)

    # Write strings
    out[string_base:string_base + len(string_data)] = string_data

    return bytes(out)


def _build_info_block(info_lists: Tuple) -> bytes:
    """Serialize INFO. Layout: header + 8 record arrays + concatenated bodies.

    Each record list is ``u32 count + u32 body_offsets[count]``; body
    offsets are 0 for empty slots, otherwise they point at the body
    bytes. Body sizes vary per record type — we treat them as opaque
    blobs (already parsed by ``_parse_info_bodies``), so we just lay
    them out contiguously after the record arrays.
    """
    record_offsets = [0] * 8
    cursor = 0x40  # header

    # Compute record-array offsets (in slot order)
    for i, lst in enumerate(info_lists):
        if lst:
            record_offsets[i] = cursor
            cursor += 4 + 4 * len(lst)

    # Now lay out bodies contiguously
    body_rels_per_list: List[List[int]] = [[] for _ in range(8)]
    body_data = bytearray()
    body_base = cursor
    for i, lst in enumerate(info_lists):
        if not lst:
            continue
        for body in lst:
            if body is None:
                body_rels_per_list[i].append(0)
            else:
                body_rels_per_list[i].append(body_base + len(body_data))
                body_data.extend(body)

    block_size = body_base + len(body_data)
    out = bytearray(block_size)
    out[0:4] = b"INFO"
    struct.pack_into("<I", out, 4, block_size)
    for i in range(8):
        struct.pack_into("<I", out, 8 + i * 4, record_offsets[i])

    for i, lst in enumerate(info_lists):
        if not lst:
            continue
        ro = record_offsets[i]
        struct.pack_into("<I", out, ro, len(lst))
        for j, br in enumerate(body_rels_per_list[i]):
            struct.pack_into("<I", out, ro + 4 + j * 4, br)

    out[body_base:body_base + len(body_data)] = body_data
    return bytes(out)


def _build_fat_block_header(
    fat_entries: List[Tuple[int, int, int, int]],
) -> bytes:
    """Serialize the FAT block. Offsets/sizes from ``fat_entries`` are
    written verbatim; the caller is responsible for filling them in
    after the FILE layout is decided."""
    count = len(fat_entries)
    out = bytearray(12 + count * 16)
    out[0:4] = b"FAT "
    struct.pack_into("<I", out, 4, len(out))
    struct.pack_into("<I", out, 8, count)
    for i, (off, sz, mem, rsv) in enumerate(fat_entries):
        struct.pack_into("<IIII", out, 12 + i * 16, off, sz, mem, rsv)
    return bytes(out)
