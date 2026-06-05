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
# Loader accepts every prior version; saver always writes the current version.
FORMAT_VERSION = 3
_ACCEPTED_VERSIONS = (1, 2, 3)
EDITOR_VERSION = "0.1.0"


StringEdit = Tuple[str, int, str]  # (region_id, vanilla_offset, new_text)
SpriteEdit = Tuple[str, int, bytes]  # (pak_name, entry_idx, new_entry_bytes)


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
) -> None:
    """Write a .romproj at `path`.

    `edited_rom_data` should be `session.serialize_all(skip_sprite_splice=True)`
    — i.e. without QoL patches and without sprite PAK splices applied — so QoL
    state lives only in the `qol` field, sprite changes live only in
    `sprite_edits`, and the byte diff captures only deliberate model edits.
    Skipping the sprite splice keeps the diff small even when a grown sprite
    would otherwise trigger a fat-shift across every later file.

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
    sprite splice path.
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
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_project(path: str) -> dict:
    """Read the project JSON. Returns a dict with diffs decoded into bytes,
    qol parsed into a QolSettings instance, string_edits as a list of
    ``(region_id, vanilla_offset, new_text)`` tuples (empty for v1 projects),
    and sprite_edits as a list of ``(pak_name, entry_idx, new_entry_bytes)``
    tuples (empty for v1/v2 projects). Caller resolves the vanilla ROM and
    verifies the hash separately."""
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
    return data
