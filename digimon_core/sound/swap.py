"""BGM swap payload builder.

Given a donor SDAT and a sequence index, produces a ``BgmSwap`` carrying
the SSEQ + flattened/trimmed SBNK + single merged SWAR ready to drop
into a DWDD bgmNN slot. The output is always single-SWAR — multi-waveArc
donors get collapsed via ``flatten`` so DWDD's loader can splice them
into one waveArc slot without sacrificing other BGMs.

The output is the byte material; the actual SDAT rebuild + ROM splice
happens later (rebuild.py for the SDAT pass, session-level splice on
ROM save).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

from .flatten import build_swar, flatten_sbnk, merge_swars
from .rebuild import info_entry_offsets, parse_sdat_blocks
from .trim import sbnk_program_map, sseq_progs


# merge_swars calls parse_swar on every buffer it's given, so unused
# waveArc slots need a valid (empty) SWAR placeholder rather than b"".
_EMPTY_SWAR = build_swar([])


@dataclass(frozen=True)
class BgmSwap:
    """A built swap payload, ready to apply to a target DWDD bgmNN slot.

    ``sseq_bytes`` is taken verbatim from the donor. ``sbnk_bytes`` is
    rewritten so every instrument points at waveArc slot 0 and references
    the merged SWAR's compact wave indices. ``swar_bytes`` is a single
    SWAR concatenating only the waves the SSEQ actually plays.

    ``trimmed_total_bytes`` is the SSEQ + SBNK + SWAR sum — the figure
    that must stay under DWDD's per-BGM cap.
    """

    target_bgm_id: int
    donor_label: str                # e.g. "BGM_EVENT_GREEN1"
    donor_game_label: str           # e.g. "awds_sound_data"
    sseq_bytes: bytes
    sbnk_bytes: bytes
    swar_bytes: bytes
    trimmed_total_bytes: int


def build_swap(
    donor_sdat: bytes,
    donor_seq_idx: int,
    target_bgm_id: int,
    *,
    donor_label: str = "",
    donor_game_label: str = "",
) -> BgmSwap:
    """Build a single-SWAR swap payload from one sequence in ``donor_sdat``.

    Steps:
      1. Resolve the SSEQ's SBNK and waveArc[] SWARs via SDAT INFO blocks.
      2. Walk the SSEQ to find which programs play; map those to the
         ``(slot, swav)`` pairs they reference in the SBNK.
      3. Trim each donor SWAR down to the used swavs (drops dead samples).
      4. Merge the per-slot SWARs into one and rewrite the SBNK so every
         instrument points at the merged SWAR's slot 0.

    Raises ``ValueError`` if the donor SBNK can't be flattened (e.g. it
    uses an instrument type the flatten code doesn't handle).
    """
    seq_bytes, sbnk_bytes, swar_buffers, used_per_slot = _gather_donor_payload(
        donor_sdat, donor_seq_idx
    )

    merged_swar, swav_remap, _kept, _total = merge_swars(swar_buffers, used_per_slot)
    new_sbnk, _touched, unknown_types = flatten_sbnk(sbnk_bytes, swav_remap)
    if unknown_types:
        raise ValueError(
            f"SBNK contains unsupported instrument types {sorted(unknown_types)}; "
            "flatten can't safely rewrite their swav/slot refs"
        )

    total = len(seq_bytes) + len(new_sbnk) + len(merged_swar)
    return BgmSwap(
        target_bgm_id=target_bgm_id,
        donor_label=donor_label,
        donor_game_label=donor_game_label,
        sseq_bytes=seq_bytes,
        sbnk_bytes=new_sbnk,
        swar_bytes=merged_swar,
        trimmed_total_bytes=total,
    )


# ---- internals -----------------------------------------------------------

def _gather_donor_payload(
    donor_sdat: bytes, seq_idx: int
) -> Tuple[bytes, bytes, List[bytes], Dict[int, Set[int]]]:
    """Resolve the SSEQ/SBNK/SWARs for ``seq_idx`` and compute used swavs.

    Returns ``(sseq_bytes, sbnk_bytes, swar_buffers, used_per_slot)`` where
    ``swar_buffers[i]`` is the donor SWAR for waveArc slot ``i`` (empty
    ``b""`` for unused slots — flatten handles missing slots via the used
    mapping). ``used_per_slot`` maps each populated slot to the set of
    swav indices the SSEQ's programs play.
    """
    _, blocks = parse_sdat_blocks(donor_sdat)
    if b"INFO" not in blocks or b"FAT " not in blocks:
        raise ValueError("donor SDAT missing INFO/FAT block")
    _, info_off, _ = blocks[b"INFO"]
    _, fat_off, _ = blocks[b"FAT "]

    info_recs = struct.unpack_from("<8I", donor_sdat, info_off + 8)
    seq_info = info_entry_offsets(donor_sdat, info_off, info_recs[0])
    bank_info = info_entry_offsets(donor_sdat, info_off, info_recs[2])
    wave_info = info_entry_offsets(donor_sdat, info_off, info_recs[3])

    if seq_idx < 0 or seq_idx >= len(seq_info) or seq_info[seq_idx] == 0:
        raise ValueError(f"donor seq_idx {seq_idx} is empty or out of range")
    ent = seq_info[seq_idx]
    seq_fid = struct.unpack_from("<H", donor_sdat, ent)[0]
    bank_no = struct.unpack_from("<H", donor_sdat, ent + 4)[0]
    if bank_no >= len(bank_info) or bank_info[bank_no] == 0:
        raise ValueError(f"donor seq_idx {seq_idx} references missing bank {bank_no}")
    bent = bank_info[bank_no]
    bank_fid = struct.unpack_from("<H", donor_sdat, bent)[0]
    arc = struct.unpack_from("<4h", donor_sdat, bent + 4)

    seq_bytes = _file_block(donor_sdat, fat_off, seq_fid)
    sbnk_bytes = _file_block(donor_sdat, fat_off, bank_fid)

    progs = sseq_progs(seq_bytes)
    prog_map, _ = sbnk_program_map(sbnk_bytes)
    kept = set(prog_map) & progs

    per_slot: Dict[int, Set[int]] = {}
    for p in kept:
        for slot, swav, _swav_off in prog_map[p][2]:
            per_slot.setdefault(slot, set()).add(swav)

    swar_buffers: List[bytes] = []
    for slot_idx, w in enumerate(arc):
        if w < 0 or w >= len(wave_info) or wave_info[w] == 0:
            if slot_idx in per_slot:
                raise ValueError(
                    f"donor SBNK references slot={slot_idx} but waveArc[{slot_idx}]"
                    " is unused or missing"
                )
            swar_buffers.append(_EMPTY_SWAR)
            continue
        wfid = struct.unpack_from("<H", donor_sdat, wave_info[w])[0]
        swar_buffers.append(_file_block(donor_sdat, fat_off, wfid))

    return seq_bytes, sbnk_bytes, swar_buffers, per_slot


def _file_block(sdat: bytes, fat_off: int, fid: int) -> bytes:
    base = fat_off + 12 + fid * 16
    off = struct.unpack_from("<I", sdat, base)[0]
    sz = struct.unpack_from("<I", sdat, base + 4)[0]
    return bytes(sdat[off:off + sz])
