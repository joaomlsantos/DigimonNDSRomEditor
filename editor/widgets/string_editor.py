"""String editor — browse and edit in-game text for one region.

Each region (msgpak block, arm9 block, overlay block) is a packed run of
strings split on [END] (FE FF) or FF FF. Pointers to individual strings
live elsewhere in the ROM and aren't repointed by the editor, so each
string's encoded length must stay within its original byte budget
(parse-time `original_byte_length`). When shortened, the terminator is
rewritten as [END] so the engine stops cleanly at the new boundary.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QSignalBlocker, QTimer
from PySide6.QtGui import QFont, QUndoStack
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import model, strings as core_strings

from ..commands import SetAttrCommand

_PREVIEW_MAX = 60  # chars shown in the list preview


def _preview(text: str) -> str:
    flat = text.replace("[BR]", " ").replace("\n", " ").replace("[END]", " / ")
    if len(flat) > _PREVIEW_MAX:
        return flat[:_PREVIEW_MAX - 1] + "…"
    return flat


def _to_display(text: str) -> str:
    """Model → text-edit form: `[BR]` becomes a real newline so the user
    sees one line per visual break."""
    return text.replace("[BR]", "\n")


def _to_canonical(text: str) -> str:
    """Text-edit → model form: real newlines (and CR/LF combos) become
    `[BR]` so the model stays in one canonical form regardless of how the
    line break was entered (typed `[BR]`, pressed Enter, pasted with \r\n)."""
    return text.replace("\r\n", "[BR]").replace("\r", "[BR]").replace("\n", "[BR]")


class StringEditor(QWidget):
    """Two-pane editor for one region: string list | text + budget meter."""

    def __init__(
        self,
        strings_in_region: List[model.GameString],
        undo_stack: QUndoStack,
        *,
        growable: bool = False,
    ):
        super().__init__()
        self._strings = strings_in_region
        self._undo_stack = undo_stack
        self._growable = growable
        self._current: Optional[model.GameString] = None

        # Debounce the undo-stack push so each keystroke doesn't trigger a
        # full redo cycle (setattr → list row repaint → budget re-encode).
        # The budget meter is still updated live from the text edit content.
        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(250)
        self._commit_timer.timeout.connect(self._commit_pending_edit)

        self._build_ui()
        self._populate_string_list()
        if self._strings:
            self._string_list.setCurrentRow(0)

    # ---- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Horizontal, self)

        # Left: search + string list
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search (substring, case-insensitive)…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
        left_layout.addWidget(self._search)

        self._summary_label = QLabel(f"{len(self._strings)} strings")
        self._summary_label.setStyleSheet("color: gray;")
        left_layout.addWidget(self._summary_label)

        self._string_list = QListWidget()
        self._string_list.currentRowChanged.connect(self._on_string_changed)
        # Monospace so the offset prefix lines up
        list_font = QFont("Consolas")
        list_font.setStyleHint(QFont.Monospace)
        self._string_list.setFont(list_font)
        left_layout.addWidget(self._string_list, stretch=1)

        # Right: text editor + meter
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 4, 4)

        self._offset_label = QLabel("")
        self._offset_label.setStyleSheet("color: gray;")
        right_layout.addWidget(self._offset_label)

        self._text_edit = QPlainTextEdit()
        edit_font = QFont("Consolas")
        edit_font.setStyleHint(QFont.Monospace)
        self._text_edit.setFont(edit_font)
        self._text_edit.textChanged.connect(self._on_text_changed)
        right_layout.addWidget(self._text_edit, stretch=1)

        self._budget_label = QLabel("")
        right_layout.addWidget(self._budget_label)

        self._marker_help = QLabel(
            "Markers: [BR]=line break, [END]=dialog end, [PLAYER_NAME], "
            "[CYAN]/[ORANGE]/[WHITE]/[GREEN]/[GREY]/[RED]/[MENU] colors, "
            "[VAR] variable insert. [?XXYY] preserves unknown bytes."
        )
        self._marker_help.setWordWrap(True)
        self._marker_help.setStyleSheet("color: gray; font-size: 11px;")
        right_layout.addWidget(self._marker_help)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 700])
        root.addWidget(splitter)

    def _populate_string_list(self) -> None:
        with QSignalBlocker(self._string_list):
            self._string_list.clear()
            for idx, s in enumerate(self._strings):
                item = QListWidgetItem(self._row_label(idx, s))
                item.setData(Qt.UserRole, idx)
                self._string_list.addItem(item)

    def _row_label(self, idx: int, s: model.GameString) -> str:
        return f"{s.offset:08X}  {_preview(s.text)}"

    def _apply_filter(self, query: str) -> None:
        q = query.strip().lower()
        visible = 0
        for row in range(self._string_list.count()):
            s = self._strings[row]
            match = (q == "") or (q in s.text.lower())
            self._string_list.setRowHidden(row, not match)
            if match:
                visible += 1
        if q:
            self._summary_label.setText(f"{visible} / {len(self._strings)} matching")
        else:
            self._summary_label.setText(f"{len(self._strings)} strings")
        # If the current selection is now hidden, jump to the first visible row.
        cur = self._string_list.currentRow()
        if cur < 0 or self._string_list.isRowHidden(cur):
            for row in range(self._string_list.count()):
                if not self._string_list.isRowHidden(row):
                    self._string_list.setCurrentRow(row)
                    return
            self._set_current(None)

    # ---- event handlers ----------------------------------------------------

    def _on_string_changed(self, row: int) -> None:
        # Flush any in-flight edit on the previous selection before switching.
        self._commit_pending_edit()
        if not (0 <= row < len(self._strings)):
            self._set_current(None)
            return
        self._set_current(self._strings[row])

    def _set_current(self, s: Optional[model.GameString]) -> None:
        self._commit_timer.stop()
        self._current = s
        if s is None:
            with QSignalBlocker(self._text_edit):
                self._text_edit.clear()
            self._offset_label.setText("")
            self._budget_label.setText("")
            self._text_edit.setEnabled(False)
            return
        self._text_edit.setEnabled(True)
        # ARM9/overlay strings have a per-string byte budget baked into the
        # ROM (their pointers are hardcoded). MSG.PAK strings have an
        # empirical 1024-byte per-string engine cap (textbox renderer
        # corrupts past it); the original byte budget no longer applies
        # because the entry is rebuilt at save time.
        if self._growable:
            self._offset_label.setText(
                f"Offset 0x{s.offset:08X} — MSG.PAK string (cap {model.MSGPAK_STRING_CAP} bytes)"
            )
        else:
            self._offset_label.setText(
                f"Offset 0x{s.offset:08X} — budget {s.original_byte_length} bytes"
            )
        with QSignalBlocker(self._text_edit):
            self._text_edit.setPlainText(_to_display(s.text))
        self._refresh_budget_for(s.text)

    def _on_text_changed(self) -> None:
        if self._current is None:
            return
        # Live budget update straight from the editor — no undo-stack push.
        self._refresh_budget_for(self._text_edit.toPlainText())
        # Defer the actual model write/redo cycle until typing pauses.
        self._commit_timer.start()

    def _commit_pending_edit(self) -> None:
        """Push a SetAttrCommand for the pending text-edit, if any."""
        self._commit_timer.stop()
        if self._current is None:
            return
        new_text = _to_canonical(self._text_edit.toPlainText())
        if new_text == self._current.text:
            return
        cmd = SetAttrCommand(
            self._current,
            "text",
            new_text,
            description="Edit string",
            on_change=self._after_text_changed,
        )
        self._undo_stack.push(cmd)

    def _after_text_changed(self) -> None:
        if self._current is None:
            return
        # Sync the editor field in case undo/redo changed it from outside.
        # Model is canonical (`[BR]`), edit shows display form (`\n`).
        display = _to_display(self._current.text)
        if self._text_edit.toPlainText() != display:
            with QSignalBlocker(self._text_edit):
                self._text_edit.setPlainText(display)
        # Refresh the list preview for the current row.
        row = self._string_list.currentRow()
        if row >= 0:
            item = self._string_list.item(row)
            if item is not None:
                item.setText(self._row_label(row, self._current))
        self._refresh_budget()

    def _refresh_budget(self) -> None:
        if self._current is None:
            self._budget_label.setText("")
            return
        self._refresh_budget_for(self._current.text)

    def _refresh_budget_for(self, text: str) -> None:
        """Update the budget meter from a raw text value (no model lookup).

        For MSG.PAK strings the meter tracks the live encoded size against
        the 1024-byte per-string engine cap. For ARM9/overlay strings the
        meter tracks the per-string byte budget as before.
        """
        if self._current is None:
            self._budget_label.setText("")
            return
        s = self._current
        if self._growable:
            try:
                # +2 for the terminator encoded_bytes_for_grow would append.
                used = core_strings.byte_length(text, terminator=None) + 2
            except core_strings.UnknownCharError as exc:
                self._budget_label.setText(f"Encode error: {exc}")
                self._budget_label.setStyleSheet("color: #b00; font-weight: bold;")
                return
            cap = model.MSGPAK_STRING_CAP
            free = cap - used
            label = f"{used} / {cap} bytes — {free} free"
            if used > cap:
                self._budget_label.setStyleSheet("color: #b00; font-weight: bold;")
                label = f"{used} / {cap} bytes — OVER CAP by {-free}"
            else:
                self._budget_label.setStyleSheet("color: gray;")
            self._budget_label.setText(label)
            return
        budget = s.original_byte_length
        try:
            # +2 for the terminator we'll write back (either original or [END]).
            used = core_strings.byte_length(text, terminator=None) + 2
        except core_strings.UnknownCharError as exc:
            self._budget_label.setText(f"Encode error: {exc}")
            self._budget_label.setStyleSheet("color: #b00; font-weight: bold;")
            return
        free = budget - used
        label = f"{used} / {budget} bytes — {free} free"
        if used > budget:
            self._budget_label.setStyleSheet("color: #b00; font-weight: bold;")
            label = f"{used} / {budget} bytes — OVER BUDGET by {-free}"
        else:
            self._budget_label.setStyleSheet("color: gray;")
        self._budget_label.setText(label)

    # ---- external navigation -----------------------------------------------

    def select_by_id(self, offset: int) -> None:
        """Jump to the row whose string starts at `offset`. Used by the
        validation footer's click-to-navigate, where `record_id` carries the
        offending string's ROM offset."""
        for row, s in enumerate(self._strings):
            if s.offset == offset:
                # Clear any active filter that would otherwise hide the row.
                if self._string_list.isRowHidden(row):
                    with QSignalBlocker(self._search):
                        self._search.clear()
                    self._apply_filter("")
                self._string_list.setCurrentRow(row)
                self._string_list.scrollToItem(self._string_list.item(row))
                return


from .validation import ValidationIssue  # noqa: E402 — bottom-of-file utility


_BUCKET_LABELS = {
    "arm9": ("ARM9 Strings", "arm9_"),
    "overlay": ("Overlay Strings", "overlay"),
    "msgpak": ("MSG.PAK Strings", "msgpak_"),
}


def _bucket_for_region(region_id: str) -> Optional[str]:
    for bucket, (_label, prefix) in _BUCKET_LABELS.items():
        if region_id.startswith(prefix):
            return bucket
    return None


def string_issues(
    string_regions: Dict[str, List[model.GameString]],
) -> List[ValidationIssue]:
    """Footer-level issues for in-game text.

    ARM9 / overlay strings: reports any string whose encoded bytes exceed
    their original byte budget (hardcoded pointers, so an over-budget
    write would clobber the next field on disk).

    MSG.PAK strings: reports any string whose encoded size exceeds the
    empirical 1024-byte per-string engine cap (the textbox renderer
    corrupts past it, regardless of content).
    """
    issues: List[ValidationIssue] = []
    for region_id, strings in string_regions.items():
        bucket = _bucket_for_region(region_id)
        if bucket is None:
            continue
        section_label, _ = _BUCKET_LABELS[bucket]
        editor_key = f"strings_bucket:{bucket}"
        if bucket == "msgpak":
            cap = model.MSGPAK_STRING_CAP
            for s in strings:
                # Fast skip: unmodified strings fit by vanilla construction.
                if s.text is s._initial_text or s.text == s._initial_text:
                    continue
                if len(s.text) * 2 + 2 <= cap:
                    continue
                size = len(s.encoded_bytes_for_grow())
                if size <= cap:
                    continue
                preview = _preview(s.text)
                issues.append(ValidationIssue(
                    section=section_label,
                    category="Over cap",
                    message=(
                        f"0x{s.offset:08X}: {size} / {cap} bytes — {preview}"
                    ),
                    editor_key=editor_key,
                    record_id=s.offset,
                ))
            continue
        for s in strings:
            # This collector runs on every undo-stack tick (every keystroke
            # editor-wide), so the per-string check must stay cheap. Parsed
            # strings always fit by construction, so identity-equal text
            # (unmodified since load) skips straight to the next entry —
            # that's the 99% case. Value equality covers undo-to-vanilla.
            t = s.text
            init = s._initial_text
            if t is init or t == init:
                continue
            # Every char/token encodes to exactly one 2-byte LE word, so
            # `len(text) * 2 + 2` is an upper bound on encoded size. If even
            # that fits, we can skip the expensive encode_string() pass.
            if len(t) * 2 + 2 <= s.original_byte_length:
                continue
            if s.fits():
                continue
            preview = _preview(s.text)
            issues.append(ValidationIssue(
                section=section_label,
                category="Over budget",
                message=(
                    f"0x{s.offset:08X}: {s.encoded_length()} / "
                    f"{s.original_byte_length} bytes — {preview}"
                ),
                editor_key=editor_key,
                record_id=s.offset,
            ))
    return issues
