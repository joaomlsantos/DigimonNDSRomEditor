"""Project file (.romproj) — byte-range diff against vanilla + QoL state.

Project files persist the user's edits without bundling the ROM bytes themselves
(copyright). A `.romproj` carries the rom_version, the vanilla ROM sha256 (so
the editor can verify the user pointed at the right ROM on reopen), the QoL
settings dataclass, a compact list of contiguous byte-range diffs against
vanilla, and a `string_edits` channel for MSG.PAK strings whose encoded length
exceeds their vanilla byte budget (those are skipped by ``serialize_all`` and
can't be represented in the equal-length byte diff — see §12.4 Phase B/F).

Opening a project = load vanilla → apply diffs → reparse model → apply
string_edits to the model. Saving a project = compute `serialize_all()` (NOT
serialize_all_with_qol — QoL goes in its own field so it doesn't compound
across round-trips) → diff vs vanilla, and snapshot over-budget MSG.PAK
strings into the string_edits channel.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import List, Tuple

from digimon_core import qol as qol_module


# v1: diffs + qol only.
# v2: adds string_edits channel for over-budget MSG.PAK strings.
# v3: adds sprite_edits channel for per-entry sprite PAK replacements
#     (keeps the byte diff small even when a sprite grows past its FAT slot).
# v4: adds btchr_appended_sidecars channel — chrsize/btchrsize u32 pairs
#     for BTCHR groups appended past vanilla 415. The corresponding PAK
#     entries ride sprite_edits at idx >= vanilla.count.
# v5: adds btmap_edits channel — per-FAT-path replacement bytes for
#     battle background (DAT/btmap/...) files edited via the Animations
#     tab import path. Keeps grown btmaps off the byte diff.
# v6: adds map_edits channel — per-FAT-path replacement bytes for field
#     map (DAT/map/...) files edited via the field-map browser's paint
#     tools. Same shape as btmap_edits; routed through the field-map FAT
#     splice on save.
# v7: adds overlay5_entry_edits channel — per-entry replacement bytes for
#     overlay 5 script entries edited via the field-map Events tab.
#     Keyed by entry index (235..498); routed through the overlay5 splice
#     on save. Entry length is invariant — only x/y windows flip — so
#     this channel never grows the overlay5 file.
# v8: adds bgm_swap_edits channel — staged donor BGM payloads (SSEQ/SBNK/
#     SWAR triples) keyed by target ``bgm_id``. Routed through the SDAT
#     rebuild + splice on ROM save; project save runs with
#     ``skip_sound_splice=True`` so a grown SDAT doesn't shift every
#     later FAT file into the byte diff.
# v9: adds bgm_addition_edits channel — staged "Add As New Entry" donor
#     payloads (same SSEQ/SBNK/SWAR triple shape) that grow the SDAT by
#     one BGM slot each. Positional: list order encodes the eventual
#     bgm_id (vanilla_seq_count + position). Routed through the same SDAT
#     splice path as bgm_swap_edits.
# v10: adds bgm_label_edits channel — user-editable friendly labels for
#      BGM slots, keyed by the SET_MUSIC sequential array id. Pure UI
#      metadata; the ROM itself doesn't carry labels, so this channel
#      never touches the byte diff or SDAT splice.
# v11: adds wild_encounter_area_edits channel — full file bytes for
#      wild-encounter areas (DAT/EC/E0XX.BIN) whose record count changed
#      (add/remove slot). Keyed by area index; routed through the
#      wild-encounter FAT splice on save. Project save runs with
#      ``skip_wild_encounter_splice=True`` so a grown area doesn't shift
#      every later FAT file into the (equal-length) byte diff.
# Loader accepts every prior version; saver always writes the current version.
FORMAT_VERSION = 11
_ACCEPTED_VERSIONS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
EDITOR_VERSION = "0.1.0"


StringEdit = Tuple[str, int, str]  # (region_id, vanilla_offset, new_text)
SpriteEdit = Tuple[str, int, bytes]  # (pak_name, entry_idx, new_entry_bytes)
BtchrAppendedSidecar = Tuple[int, int]  # (chrsize_word, btchrsize_value)
BtmapEdit = Tuple[str, bytes]  # (fat_path, file_bytes)
MapEdit = Tuple[str, bytes]  # (fat_path, file_bytes) — DAT/map/* overrides
Overlay5EntryEdit = Tuple[int, bytes]  # (entry_ix, entry_bytes)
# (target_bgm_id, donor_label, donor_game_label, sseq_bytes, sbnk_bytes, swar_bytes)
BgmSwapEdit = Tuple[int, str, str, bytes, bytes, bytes]
# (donor_label, donor_game_label, sseq_bytes, sbnk_bytes, swar_bytes)
BgmAdditionEdit = Tuple[str, str, bytes, bytes, bytes]
BgmLabelEdit = Tuple[int, str]  # (music_id / SET_MUSIC array id, label)
WildEncounterAreaEdit = Tuple[int, bytes]  # (area_index, full_file_bytes)


def vanilla_sha256(rom_bytes: bytes) -> str:
    """Full SHA-256 hex of the vanilla ROM. Used both to identify the ROM a
    project was built against (stored in the .romproj) and to verify the user
    pointed at the right ROM on reopen."""
    return hashlib.sha256(rom_bytes).hexdigest()


def compute_byte_diff(original: bytes, edited: bytes) -> List[Tuple[int, bytes]]:
    """Walk both inputs in lockstep; emit one (offset, bytes) per contiguous
    run of mismatches. Inputs must be equal-length — same-ROM-size is a hard
    invariant of the serialiser, so an unequal length signals a bug upstream.
    """
    if len(original) != len(edited):
        raise ValueError(
            f"diff requires equal-length inputs (orig={len(original)}, edited={len(edited)})"
        )
    diffs: List[Tuple[int, bytes]] = []
    n = len(original)
    i = 0
    while i < n:
        if original[i] != edited[i]:
            start = i
            while i < n and original[i] != edited[i]:
                i += 1
            diffs.append((start, bytes(edited[start:i])))
        else:
            i += 1
    return diffs


def apply_byte_diff(rom: bytearray, diffs: List[Tuple[int, bytes]]) -> None:
    """Splice each (offset, bytes) chunk back onto `rom` in-place."""
    for off, chunk in diffs:
        rom[off:off + len(chunk)] = chunk


def _serialize_qol(settings: qol_module.QolSettings) -> dict:
    return asdict(settings)


def _deserialize_qol(data: dict) -> qol_module.QolSettings:
    # Tolerant of missing keys (defaults filled in) so a .romproj from a
    # slightly older editor still loads cleanly.
    out = qol_module.QolSettings()
    for k, v in data.items():
        if hasattr(out, k):
            setattr(out, k, v)
    return out


def save_project(
    path: str,
    *,
    rom_version: str,
    vanilla_rom_data: bytes,
    edited_rom_data: bytes,
    qol: qol_module.QolSettings,
    string_edits: List[StringEdit] = (),
    sprite_edits: List[SpriteEdit] = (),
    btchr_appended_sidecars: List[BtchrAppendedSidecar] = (),
    btmap_edits: List[BtmapEdit] = (),
    map_edits: List[MapEdit] = (),
    overlay5_entry_edits: List[Overlay5EntryEdit] = (),
    bgm_swap_edits: List[BgmSwapEdit] = (),
    bgm_addition_edits: List[BgmAdditionEdit] = (),
    bgm_label_edits: List[BgmLabelEdit] = (),
    wild_encounter_area_edits: List[WildEncounterAreaEdit] = (),
) -> None:
    """Write a .romproj at `path`.

    `edited_rom_data` should be
    `session.serialize_all(skip_sprite_splice=True, skip_btmap_splice=True,
    skip_map_splice=True)`
    — i.e. without QoL patches and without sprite PAK, btmap, or field-map
    splices applied — so QoL state lives only in the `qol` field, sprite
    changes live only in `sprite_edits`, btmap changes live only in
    `btmap_edits`, field-map changes live only in `map_edits`, and the byte
    diff captures only deliberate model edits. Skipping all three FAT splices
    keeps the diff small even when a grown file would otherwise trigger a
    fat-shift across every later file.

    `string_edits` carries over-budget MSG.PAK strings as
    ``(region_id, vanilla_offset, new_text)`` triples. ``serialize_all`` skips
    these strings (their grown encoding can't be written in-place without
    corrupting neighbours), so they wouldn't otherwise appear in the byte
    diff. On reopen they're applied to the reparsed model after the byte diff
    lands, and the next ROM save runs them through the §12 grow path.

    `sprite_edits` carries replaced sprite PAK entries as
    ``(pak_name, entry_idx, new_entry_bytes)`` triples. On reopen they're
    applied to the session via ``apply_sprite_pak_edits`` after the byte diff
    and string edits land; the next ROM save runs them through the normal
    sprite splice path. Entries with ``entry_idx >= vanilla pak count`` are
    appends (extend the pak by one slot); they must arrive in order.

    `btchr_appended_sidecars` carries chrsize/btchrsize u32 pairs for BTCHR
    groups appended past vanilla 415 — parallel to the BTCHR.PAK appended
    entries in `sprite_edits`. Required so the (PAK count, chrsize length,
    btchrsize length) triple stays consistent on reload.

    `btmap_edits` carries per-path battle-background overrides as
    ``(fat_path, file_bytes)`` tuples. On reopen they're applied to the
    session via ``apply_btmap_file_edits`` after the byte diff, sprite,
    and string edits land; the next ROM save runs them through the btmap
    FAT splice path.

    `map_edits` carries per-path field-map overrides as
    ``(fat_path, file_bytes)`` tuples — same shape as ``btmap_edits``,
    routed through ``_apply_map_splice`` on save and replayed via
    ``apply_map_file_edits`` on load.

    `overlay5_entry_edits` carries per-entry overlay5 script overrides
    as ``(entry_ix, entry_bytes)`` tuples. Entry length is invariant, so
    these never grow the overlay5 file — they're skipped in the
    serialize step and replayed via ``apply_overlay5_entry_edits`` on
    load before the next save runs them through the overlay5 splice.

    `bgm_swap_edits` carries staged donor BGM payloads as
    ``(target_bgm_id, donor_label, donor_game_label, sseq, sbnk, swar)``
    tuples. Routed through the SDAT rebuild + ROM splice on save;
    project save passes ``skip_sound_splice=True`` so a grown SDAT
    doesn't shift every later FAT file into the byte diff. Replayed
    via ``apply_bgm_swap_edits`` on load.

    `bgm_addition_edits` carries staged "Add As New Entry" donor payloads
    as ``(donor_label, donor_game_label, sseq, sbnk, swar)`` tuples.
    Positional: list order encodes the eventual bgm_id
    (``vanilla_seq_count + position``). Routed through the same SDAT
    rebuild + splice on save; replayed via ``apply_bgm_addition_edits``
    on load.
    """
    diffs = compute_byte_diff(vanilla_rom_data, edited_rom_data)
    payload = {
        "format_version": FORMAT_VERSION,
        "editor_version": EDITOR_VERSION,
        "rom_version": rom_version,
        "vanilla_rom_sha256": vanilla_sha256(vanilla_rom_data),
        "qol": _serialize_qol(qol),
        "diffs": [
            {"offset": off, "bytes": base64.b64encode(chunk).decode("ascii")}
            for off, chunk in diffs
        ],
        "string_edits": [
            {"region": region, "offset": off, "text": text}
            for region, off, text in string_edits
        ],
        "sprite_edits": [
            {"pak": pak, "idx": idx, "bytes": base64.b64encode(data).decode("ascii")}
            for pak, idx, data in sprite_edits
        ],
        "btchr_appended_sidecars": [
            {"chrsize": chrsize_word, "btchrsize": btchrsize_value}
            for chrsize_word, btchrsize_value in btchr_appended_sidecars
        ],
        "btmap_edits": [
            {"path": path, "bytes": base64.b64encode(data).decode("ascii")}
            for path, data in btmap_edits
        ],
        "map_edits": [
            {"path": path, "bytes": base64.b64encode(data).decode("ascii")}
            for path, data in map_edits
        ],
        "overlay5_entry_edits": [
            {"entry_ix": ix, "bytes": base64.b64encode(data).decode("ascii")}
            for ix, data in overlay5_entry_edits
        ],
        "bgm_swap_edits": [
            {
                "bgm_id": bgm_id,
                "donor_label": donor_label,
                "donor_game_label": donor_game_label,
                "sseq": base64.b64encode(sseq).decode("ascii"),
                "sbnk": base64.b64encode(sbnk).decode("ascii"),
                "swar": base64.b64encode(swar).decode("ascii"),
            }
            for bgm_id, donor_label, donor_game_label, sseq, sbnk, swar in bgm_swap_edits
        ],
        "bgm_addition_edits": [
            {
                "donor_label": donor_label,
                "donor_game_label": donor_game_label,
                "sseq": base64.b64encode(sseq).decode("ascii"),
                "sbnk": base64.b64encode(sbnk).decode("ascii"),
                "swar": base64.b64encode(swar).decode("ascii"),
            }
            for donor_label, donor_game_label, sseq, sbnk, swar in bgm_addition_edits
        ],
        "bgm_label_edits": [
            {"music_id": music_id, "label": label}
            for music_id, label in bgm_label_edits
        ],
        "wild_encounter_area_edits": [
            {"area_ix": area_ix, "bytes": base64.b64encode(data).decode("ascii")}
            for area_ix, data in wild_encounter_area_edits
        ],
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_project(path: str) -> dict:
    """Read the project JSON. Returns a dict with diffs decoded into bytes,
    qol parsed into a QolSettings instance, string_edits as a list of
    ``(region_id, vanilla_offset, new_text)`` tuples (empty for v1 projects),
    sprite_edits as a list of ``(pak_name, entry_idx, new_entry_bytes)``
    tuples (empty for v1/v2 projects), btmap_edits as a list of
    ``(fat_path, file_bytes)`` tuples (empty for v1-v4 projects),
    map_edits as a list of ``(fat_path, file_bytes)`` tuples (empty for
    v1-v5 projects), overlay5_entry_edits as a list of
    ``(entry_ix, entry_bytes)`` tuples (empty for v1-v6 projects), and
    bgm_swap_edits as a list of ``(target_bgm_id, donor_label,
    donor_game_label, sseq_bytes, sbnk_bytes, swar_bytes)`` tuples
    (empty for v1-v7 projects).
    Caller resolves the vanilla ROM and verifies the hash separately."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    fmt = data.get("format_version")
    if fmt not in _ACCEPTED_VERSIONS:
        raise ValueError(
            f"unsupported project file format_version {fmt!r} "
            f"(this editor reads format_versions={list(_ACCEPTED_VERSIONS)})"
        )
    data["diffs"] = [
        (entry["offset"], base64.b64decode(entry["bytes"]))
        for entry in data.get("diffs", [])
    ]
    data["qol"] = _deserialize_qol(data.get("qol", {}))
    data["string_edits"] = [
        (entry["region"], entry["offset"], entry["text"])
        for entry in data.get("string_edits", [])
    ]
    data["sprite_edits"] = [
        (entry["pak"], entry["idx"], base64.b64decode(entry["bytes"]))
        for entry in data.get("sprite_edits", [])
    ]
    data["btchr_appended_sidecars"] = [
        (entry["chrsize"], entry["btchrsize"])
        for entry in data.get("btchr_appended_sidecars", [])
    ]
    data["btmap_edits"] = [
        (entry["path"], base64.b64decode(entry["bytes"]))
        for entry in data.get("btmap_edits", [])
    ]
    data["map_edits"] = [
        (entry["path"], base64.b64decode(entry["bytes"]))
        for entry in data.get("map_edits", [])
    ]
    data["overlay5_entry_edits"] = [
        (entry["entry_ix"], base64.b64decode(entry["bytes"]))
        for entry in data.get("overlay5_entry_edits", [])
    ]
    data["bgm_swap_edits"] = [
        (
            entry["bgm_id"],
            entry.get("donor_label", ""),
            entry.get("donor_game_label", ""),
            base64.b64decode(entry["sseq"]),
            base64.b64decode(entry["sbnk"]),
            base64.b64decode(entry["swar"]),
        )
        for entry in data.get("bgm_swap_edits", [])
    ]
    data["bgm_addition_edits"] = [
        (
            entry.get("donor_label", ""),
            entry.get("donor_game_label", ""),
            base64.b64decode(entry["sseq"]),
            base64.b64decode(entry["sbnk"]),
            base64.b64decode(entry["swar"]),
        )
        for entry in data.get("bgm_addition_edits", [])
    ]
    data["bgm_label_edits"] = [
        (int(entry["music_id"]) & 0xFFFF, entry["label"])
        for entry in data.get("bgm_label_edits", [])
    ]
    data["wild_encounter_area_edits"] = [
        (entry["area_ix"], base64.b64decode(entry["bytes"]))
        for entry in data.get("wild_encounter_area_edits", [])
    ]
    return data
