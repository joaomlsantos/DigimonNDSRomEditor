"""Donor-SDAT audit — library form of ``audit_donor_sdat.py``.

Given raw bytes from any NDS SDAT, enumerate the candidate BGM sequences
and compute, for each, the post-trim runtime footprint that DWDD would
need (SSEQ + trimmed SBNK + summed trimmed waveArc SWARs). Rows with
``fits_cap = True`` are importable into a 166 KB BGM slot in the editor.

This is read-only: no swap bytes are constructed here. Phase 3 will lift
the trimmed payload via the same primitives in ``trim.py`` plus the
flatten step from the root-level ``sbnk_flatten.py``.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional

from .trim import build_trimmed_swar, sbnk_program_map, sseq_progs


CAP_BYTES = 166 * 1024


@dataclass(frozen=True)
class DonorBgmRow:
    """One candidate BGM in a donor SDAT, post-trim sizing.

    ``trimmed_total`` is the SSEQ+SBNK+ΣSWAR figure to compare against
    the per-BGM cap. ``parse_failed`` flags rows whose SSEQ or SBNK
    didn't walk cleanly — those can't be imported until someone fixes
    the walker for that case.
    """

    name: str
    seq_index: int
    bank_name: str
    wave_names: List[str]
    sseq_size: int
    sbnk_size: int           # original (not trimmed — SBNK keeps its size; we just zero records)
    swar_trimmed_total: int
    swar_original_total: int
    trimmed_total: int       # sseq_size + sbnk_size + swar_trimmed_total
    progs_used: int
    slots_used: int
    parse_failed: bool
    fits_cap: bool


def audit_donor(
    sdat: bytes,
    name_filter: str = "",
    exclude: str = "SE_",
) -> List[DonorBgmRow]:
    """Walk every named (or numbered) sequence in ``sdat`` and return rows.

    ``name_filter`` is case-insensitive substring; ``exclude`` is a
    case-insensitive substring rejected even if it matches the filter.
    Defaults surface every sequence except those whose name contains
    ``SE_`` (sound-effect convention used by AWDS/DWDD). Pass a tighter
    ``name_filter`` (e.g. ``"BGM"``) to narrow further, or empty
    ``exclude`` to surface SFX as well.
    """
    if sdat[:4] != b"SDAT":
        raise ValueError("not a SDAT container")

    parsed = _parse_sdat(sdat)
    seq_names = parsed["seq_names"]
    bank_names = parsed["bank_names"]
    wave_names = parsed["wave_names"]
    seq_info = parsed["seq_info"]
    bank_info = parsed["bank_info"]
    wave_info = parsed["wave_info"]
    fat_off = parsed["fat"]

    rows: List[DonorBgmRow] = []
    name_filter_u = name_filter.upper()
    exclude_u = exclude.upper()

    for idx, name in enumerate(seq_names):
        if not name:
            continue
        if name_filter_u and name_filter_u not in name.upper():
            continue
        if exclude_u and exclude_u in name.upper():
            continue
        if seq_info[idx] == 0:
            continue

        ent = seq_info[idx]
        seq_fid = struct.unpack_from("<H", sdat, ent)[0]
        bank_no = struct.unpack_from("<H", sdat, ent + 4)[0]
        if bank_no >= len(bank_info) or bank_info[bank_no] == 0:
            continue
        bent = bank_info[bank_no]
        bank_fid = struct.unpack_from("<H", sdat, bent)[0]
        arc = struct.unpack_from("<4h", sdat, bent + 4)

        sseq_bytes, sseq_sz = _file_block(sdat, fat_off, seq_fid)
        sbnk_bytes, sbnk_sz = _file_block(sdat, fat_off, bank_fid)

        try:
            progs = sseq_progs(sseq_bytes)
            prog_map, _ = sbnk_program_map(sbnk_bytes)
        except Exception:
            rows.append(DonorBgmRow(
                name=name,
                seq_index=idx,
                bank_name=bank_names[bank_no] if bank_no < len(bank_names) else "",
                wave_names=[],
                sseq_size=sseq_sz,
                sbnk_size=sbnk_sz,
                swar_trimmed_total=0,
                swar_original_total=0,
                trimmed_total=sseq_sz + sbnk_sz,
                progs_used=0,
                slots_used=0,
                parse_failed=True,
                fits_cap=False,
            ))
            continue

        kept = set(prog_map) & progs
        per_slot: dict = {}
        for p in kept:
            for slot, swav, _swav_off in prog_map[p][2]:
                per_slot.setdefault(slot, set()).add(swav)

        used_wave_names: List[str] = []
        swar_trimmed = 0
        swar_orig = 0
        slots_used = 0
        for slot_idx, w in enumerate(arc):
            if w < 0 or w >= len(wave_info) or wave_info[w] == 0:
                continue
            wfid = struct.unpack_from("<H", sdat, wave_info[w])[0]
            swar_bytes, w_sz = _file_block(sdat, fat_off, wfid)
            swar_orig += w_sz
            if w < len(wave_names):
                used_wave_names.append(wave_names[w])
            if not swar_bytes.startswith(b"SWAR"):
                continue
            keep = sorted(per_slot.get(slot_idx, set()))
            try:
                new_swar, _ = build_trimmed_swar(swar_bytes, keep)
                swar_trimmed += len(new_swar)
                if keep:
                    slots_used += 1
            except Exception:
                swar_trimmed += w_sz

        total = sseq_sz + sbnk_sz + swar_trimmed
        rows.append(DonorBgmRow(
            name=name,
            seq_index=idx,
            bank_name=bank_names[bank_no] if bank_no < len(bank_names) else "",
            wave_names=used_wave_names,
            sseq_size=sseq_sz,
            sbnk_size=sbnk_sz,
            swar_trimmed_total=swar_trimmed,
            swar_original_total=swar_orig,
            trimmed_total=total,
            progs_used=len(kept),
            slots_used=slots_used,
            parse_failed=False,
            fits_cap=total <= CAP_BYTES,
        ))

    rows.sort(key=lambda r: (not r.fits_cap, r.trimmed_total))
    return rows


# ---- SDAT parse helpers --------------------------------------------------

def _parse_sdat(sdat: bytes) -> dict:
    n_blocks = struct.unpack_from("<H", sdat, 14)[0]
    blocks: dict = {}
    for i in range(n_blocks):
        off, _sz = struct.unpack_from("<II", sdat, 0x10 + i * 8)
        if off:
            blocks[bytes(sdat[off:off + 4])] = off
    if b"INFO" not in blocks or b"FAT " not in blocks:
        raise ValueError("SDAT missing INFO/FAT block")
    info_off = blocks[b"INFO"]
    fat_off = blocks[b"FAT "]
    info_recs = struct.unpack_from("<8I", sdat, info_off + 8)

    def info_list(rel: int) -> List[int]:
        base = info_off + rel
        n = struct.unpack_from("<I", sdat, base)[0]
        return [
            info_off + o if o else 0
            for o in struct.unpack_from(f"<{n}I", sdat, base + 4)
        ]

    seq_info = info_list(info_recs[0])
    bank_info = info_list(info_recs[2])
    wave_info = info_list(info_recs[3])

    if b"SYMB" in blocks:
        symb_off = blocks[b"SYMB"]
        symb_recs = struct.unpack_from("<8I", sdat, symb_off + 8)

        def names(rel: int) -> List[str]:
            base = symb_off + rel
            n = struct.unpack_from("<I", sdat, base)[0]
            out: List[str] = []
            for i in range(n):
                sr = struct.unpack_from("<I", sdat, base + 4 + i * 4)[0]
                if sr == 0:
                    out.append("")
                    continue
                a = symb_off + sr
                end = sdat.find(b"\x00", a)
                out.append(bytes(sdat[a:end]).decode("ascii", "replace"))
            return out

        seq_names = names(symb_recs[0])
        bank_names = names(symb_recs[2])
        wave_names = names(symb_recs[3])
    else:
        seq_names = [f"SEQ_{i:04d}" for i in range(len(seq_info))]
        bank_names = [f"BANK_{i:04d}" for i in range(len(bank_info))]
        wave_names = [f"WAVE_{i:04d}" for i in range(len(wave_info))]

    return {
        "fat": fat_off,
        "seq_info": seq_info,
        "bank_info": bank_info,
        "wave_info": wave_info,
        "seq_names": seq_names,
        "bank_names": bank_names,
        "wave_names": wave_names,
    }


def _file_block(sdat: bytes, fat_off: int, fid: int) -> tuple:
    base = fat_off + 12 + fid * 16
    off = struct.unpack_from("<I", sdat, base)[0]
    sz = struct.unpack_from("<I", sdat, base + 4)[0]
    return sdat[off:off + sz], sz
