"""NDS SDAT container parsing — vanilla-BGM enumeration for the sound editor.

The full byte-level format documentation lives in
``research_docs/claude_notes/bgm_injection_pipeline.md``. This module covers
only what Phase 1 of the editor needs: list every ``bgmNN`` SSEQ in DWDD's
``dat/snd/sound_data.sdat`` with its SBNK + waveArc-summed SWAR footprint.

No mutation, no rebuilding — that lives in later phases via the standalone
``sdat_swap_bgm.py`` / ``sseq_aware_trim.py`` tools at the repo root.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional


SDAT_PATH = "dat/snd/sound_data.sdat"


@dataclass(frozen=True)
class BgmSummary:
    """One ``bgmNN`` slot's parsed footprint inside the vanilla SDAT.

    ``total_size`` is what the runtime BGM buffer has to hold (SSEQ +
    SBNK + every populated waveArc SWAR). It's the number the editor
    compares against the ~166 KB per-BGM cap when deciding which donor
    candidates are importable.
    """

    bgm_id: int                 # parsed from the trailing digits of seq_name
    seq_name: str               # e.g. "bgm11"
    bank_name: str              # e.g. "BANK_BGM11"
    wave_names: List[str]       # populated waveArc slots, in slot order
    sseq_size: int
    sbnk_size: int
    swar_total_size: int        # summed across populated waveArc entries
    total_size: int             # sbnk_size + swar_total_size


def list_bgms(sdat: bytes) -> List[BgmSummary]:
    """Enumerate every ``bgmNN`` sequence in the SDAT.

    Returns an empty list if the SDAT has no SYMB block (e.g. MKDS-style
    donor SDATs) — vanilla DWDD always ships symbols, so the editor's
    left list relies on them. Donor audits in later phases will handle
    the nameless case separately.
    """
    if sdat[:4] != b"SDAT":
        raise ValueError("not a SDAT container")

    blocks = _read_block_table(sdat)
    symb_off = blocks.get(b"SYMB")
    info_off = blocks[b"INFO"]
    fat_off = blocks[b"FAT "]
    if symb_off is None:
        return []

    symb_recs = struct.unpack_from("<8I", sdat, symb_off + 8)
    info_recs = struct.unpack_from("<8I", sdat, info_off + 8)

    seq_names = _read_name_list(sdat, symb_off, symb_recs[0])
    bank_names = _read_name_list(sdat, symb_off, symb_recs[2])
    wave_names = _read_name_list(sdat, symb_off, symb_recs[3])

    seq_info = _read_info_list(sdat, info_off, info_recs[0])
    bank_info = _read_info_list(sdat, info_off, info_recs[2])
    wave_info = _read_info_list(sdat, info_off, info_recs[3])

    out: List[BgmSummary] = []
    for seq_idx, name in enumerate(seq_names):
        bgm_id = _parse_bgm_id(name)
        if bgm_id is None:
            continue
        ent = seq_info[seq_idx]
        if ent == 0:
            continue
        seq_fid = struct.unpack_from("<H", sdat, ent)[0]
        bank_no = struct.unpack_from("<H", sdat, ent + 4)[0]
        if bank_no >= len(bank_info) or bank_info[bank_no] == 0:
            continue
        bent = bank_info[bank_no]
        bank_fid = struct.unpack_from("<H", sdat, bent)[0]
        # waveArc is 4 × s16 INFO-record indices (not FAT ids). -1 = unused.
        arc = struct.unpack_from("<4h", sdat, bent + 4)

        sseq_size = _fat_size(sdat, fat_off, seq_fid)
        sbnk_size = _fat_size(sdat, fat_off, bank_fid)

        used_waves: List[str] = []
        swar_total = 0
        for slot_wi in arc:
            if slot_wi < 0 or slot_wi >= len(wave_info):
                continue
            went = wave_info[slot_wi]
            if went == 0:
                continue
            wfid = struct.unpack_from("<H", sdat, went)[0]
            swar_total += _fat_size(sdat, fat_off, wfid)
            used_waves.append(wave_names[slot_wi])

        out.append(
            BgmSummary(
                bgm_id=bgm_id,
                seq_name=name,
                bank_name=bank_names[bank_no],
                wave_names=used_waves,
                sseq_size=sseq_size,
                sbnk_size=sbnk_size,
                swar_total_size=swar_total,
                total_size=sseq_size + sbnk_size + swar_total,
            )
        )

    out.sort(key=lambda b: b.bgm_id)
    return out


# ---- helpers -------------------------------------------------------------

def _read_block_table(sdat: bytes) -> dict:
    n_blocks = struct.unpack_from("<H", sdat, 14)[0]
    blocks = {}
    for i in range(n_blocks):
        off, _sz = struct.unpack_from("<II", sdat, 0x10 + i * 8)
        if off:
            blocks[bytes(sdat[off:off + 4])] = off
    return blocks


def _read_name_list(sdat: bytes, symb_off: int, list_rel: int) -> List[str]:
    if list_rel == 0:
        return []
    base = symb_off + list_rel
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


def _read_info_list(sdat: bytes, info_off: int, list_rel: int) -> List[int]:
    base = info_off + list_rel
    n = struct.unpack_from("<I", sdat, base)[0]
    return [
        info_off + o if o else 0
        for o in struct.unpack_from(f"<{n}I", sdat, base + 4)
    ]


def _fat_size(sdat: bytes, fat_off: int, fid: int) -> int:
    return struct.unpack_from("<I", sdat, fat_off + 12 + fid * 16 + 4)[0]


def _parse_bgm_id(name: str) -> Optional[int]:
    """Return the integer id of a ``bgmNN`` sequence, or None for non-BGMs.

    DWDD names its music as ``bgm00``..``bgm69`` (with gaps). Sound-effect
    and system sequences use other prefixes (``ses_*`` etc.); skip those.
    """
    if not name.startswith("bgm"):
        return None
    tail = name[3:]
    if not tail.isdigit():
        return None
    return int(tail)
