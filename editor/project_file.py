"""Project file (.romproj) — byte-range diff against vanilla + QoL state.

Project files persist the user's edits without bundling the ROM bytes themselves
(copyright). A `.romproj` carries the rom_version, the vanilla ROM sha256 (so
the editor can verify the user pointed at the right ROM on reopen), the QoL
settings dataclass, and a compact list of contiguous byte-range diffs against
vanilla.

Opening a project = load vanilla → apply diffs → reparse model. Saving a
project = compute `serialize_all()` (NOT serialize_all_with_qol — QoL goes in
its own field so it doesn't compound across round-trips) → diff vs vanilla.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import List, Tuple

from digimon_core import qol as qol_module


FORMAT_VERSION = 1
EDITOR_VERSION = "0.1.0"


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
) -> None:
    """Write a .romproj at `path`.

    `edited_rom_data` should be `session.serialize_all()` — i.e. without QoL
    patches applied — so QoL state lives only in the `qol` field and the byte
    diff captures only deliberate model edits.
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
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_project(path: str) -> dict:
    """Read the project JSON. Returns a dict with diffs decoded into bytes and
    qol parsed into a QolSettings instance. Caller resolves the vanilla ROM
    and verifies the hash separately."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    fmt = data.get("format_version")
    if fmt != FORMAT_VERSION:
        raise ValueError(
            f"unsupported project file format_version {fmt!r} "
            f"(this editor reads format_version={FORMAT_VERSION})"
        )
    data["diffs"] = [
        (entry["offset"], base64.b64decode(entry["bytes"]))
        for entry in data.get("diffs", [])
    ]
    data["qol"] = _deserialize_qol(data.get("qol", {}))
    return data
