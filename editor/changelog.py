"""Per-ROM changelog — one detail file per save, indexed by a running log.

Triggered from `_on_save` / `_on_save_as` in the main window. Captures the
`QUndoCommand.text()` of every command between the previous clean index and
the current stack position, so each entry corresponds to the actual edits a
particular save persisted (not the user's whole session history).

Layout under `QStandardPaths.AppDataLocation`:

    changelogs/<rom_hash>/
        index.txt              # one line per save: timestamp · edits · path
        <YYYY-MM-DD_HH-MM-SS>.log   # detail: timestamped header + bullet list

`<rom_hash>` keys off the *vanilla* ROM bytes so renaming the .nds, or
using Save As to fork an edited copy, doesn't split history across folders.
Action-text only for v1 — no before/after value capture.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QUndoStack


def rom_hash(rom_bytes: bytes) -> str:
    """16-char SHA-256 prefix — collision-safe at single-user scale."""
    return hashlib.sha256(rom_bytes).hexdigest()[:16]


def changelog_root() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    return Path(base) / "changelogs"


def changelog_dir(rom_h: str) -> Path:
    return changelog_root() / rom_h


def _collect_edits(stack: QUndoStack, from_ix: int, to_ix: int) -> List[str]:
    """Return command texts between two stack indices.

    Forward (save after edits): texts of commands [from_ix, to_ix).
    Backward (save after undo): "[undone] <text>" for commands [to_ix, from_ix)
    — those are the edits that got rolled back since the last clean point.
    """
    entries: List[str] = []
    if to_ix >= from_ix:
        for i in range(from_ix, to_ix):
            cmd = stack.command(i)
            entries.append(cmd.text() if cmd is not None else "(unnamed)")
    else:
        for i in range(to_ix, from_ix):
            cmd = stack.command(i)
            text = cmd.text() if cmd is not None else "(unnamed)"
            entries.append(f"[undone] {text}")
    return entries


def record_save(
    rom_bytes: bytes,
    saved_path: str,
    stack: QUndoStack,
    prev_clean_ix: int,
) -> Optional[Path]:
    """Append a save entry to <rom_hash>/index.txt and write a detail file.

    Returns the detail-file path on success, or None if the save produced no
    edits (Save As of a clean session) or the write failed. Errors are
    swallowed: the user's save already succeeded, and the changelog is
    advisory — we don't want a transient I/O issue to surface as a popup.
    """
    try:
        # cleanIndex() returns -1 if setClean() has never been called; treat
        # as 0 so the first save after load captures everything since clear().
        from_ix = max(prev_clean_ix, 0)
        entries = _collect_edits(stack, from_ix, stack.index())
        if not entries:
            return None
        d = changelog_dir(rom_hash(rom_bytes))
        d.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        ts_file = now.strftime("%Y-%m-%d_%H-%M-%S")
        ts_human = now.strftime("%Y-%m-%d %H:%M:%S")
        # Append -2, -3, … if two saves land in the same second so a rapid
        # Ctrl+S doesn't silently overwrite the previous detail file.
        detail = d / f"{ts_file}.log"
        suffix = 2
        while detail.exists():
            detail = d / f"{ts_file}-{suffix}.log"
            suffix += 1
        with detail.open("w", encoding="utf-8") as f:
            f.write(f"# Save at {ts_human}\n")
            f.write(f"# ROM: {saved_path}\n\n")
            for entry in entries:
                f.write(f"- {entry}\n")
        count_label = "1 edit" if len(entries) == 1 else f"{len(entries)} edits"
        with (d / "index.txt").open("a", encoding="utf-8") as f:
            f.write(f"{ts_human} \u00b7 {count_label} \u00b7 {saved_path}\n")
        return detail
    except OSError:
        return None
