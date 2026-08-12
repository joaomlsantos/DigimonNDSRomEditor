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
from PySide6.QtGui import QColor, QFont, QUndoStack
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
        session=None,
        cursor_key: Optional[str] = None,
    ):
        super().__init__()
        self._strings = strings_in_region
        self._undo_stack = undo_stack
        self._growable = growable
        self._session = session
        self._cursor_key = cursor_key
        self._current: Optional[model.GameString] = None
        # MSG.PAK strings carry page/group/msg_id; when present the list is
        # rendered grouped by page (id-range separators) with a jump-to-id box.
        self._paged = any(getattr(s, "page", None) is not None for s in self._strings)
        self._row_of_string: Dict[int, int] = {}

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
            remembered = (
                self._session.recall_selection(self._cursor_key)
                if self._session is not None and self._cursor_key is not None
                else None
            )
            target_idx = 0
            if remembered is not None and 0 <= int(remembered) < len(self._strings):
                target_idx = int(remembered)
            self._select_string_index(target_idx)

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

        if self._paged:
            self._jump = QLineEdit()
            self._jump.setPlaceholderText("Jump to id (e.g. 9038 or 0x234E)…")
            self._jump.setClearButtonEnabled(True)
            self._jump.returnPressed.connect(self._on_jump_to_id)
            left_layout.addWidget(self._jump)

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
            self._row_of_string = {}
            prev_page = None
            for idx, s in enumerate(self._strings):
                page = getattr(s, "page", None)
                if page is not None and page != prev_page:
                    header = QListWidgetItem(self._page_header_label(page))
                    header.setFlags(Qt.ItemIsEnabled)  # visible separator, not selectable
                    header.setData(Qt.UserRole, None)
                    hfont = header.font()
                    hfont.setBold(True)
                    header.setFont(hfont)
                    header.setForeground(QColor("#4a90d9"))
                    self._string_list.addItem(header)
                    prev_page = page
                row = self._string_list.count()
                item = QListWidgetItem(self._row_label(idx, s))
                item.setData(Qt.UserRole, idx)
                self._string_list.addItem(item)
                self._row_of_string[idx] = row

    def _row_label(self, idx: int, s: model.GameString) -> str:
        mid = getattr(s, "msg_id", None)
        if mid is not None:
            return f"{mid:>5} 0x{mid:04X}  {_preview(s.text)}"
        page = getattr(s, "page", None)
        if page is not None:
            return f"   p{page} g{getattr(s, 'group', '?')}  {_preview(s.text)}"
        return f"{s.offset:08X}  {_preview(s.text)}"

    def _page_header_label(self, page: int) -> str:
        if page >= 0x22:
            base = (page - 0x22) * 100
            return f"──  Page {page}  ·  ids {base}–{base + 99}  ──"
        return f"──  Page {page}  ·  (no message id)  ──"

    def _string_index_at_row(self, row: int) -> Optional[int]:
        """Model index for a list row, or None for page-header rows / out of range."""
        if not (0 <= row < self._string_list.count()):
            return None
        item = self._string_list.item(row)
        if item is None:
            return None
        idx = item.data(Qt.UserRole)
        if idx is None or not (0 <= int(idx) < len(self._strings)):
            return None
        return int(idx)

    def _select_string_index(self, idx: int) -> None:
        row = self._row_of_string.get(idx)
        if row is not None:
            self._string_list.setCurrentRow(row)
            self._string_list.scrollToItem(self._string_list.item(row))

    def _apply_filter(self, query: str) -> None:
        q = query.strip().lower()
        visible = 0
        for row in range(self._string_list.count()):
            idx = self._string_index_at_row(row)
            if idx is None:  # page header — hidden while a filter is active
                self._string_list.setRowHidden(row, bool(q))
                continue
            s = self._strings[idx]
            match = (q == "") or (q in s.text.lower())
            self._string_list.setRowHidden(row, not match)
            if match:
                visible += 1
        if q:
            self._summary_label.setText(f"{visible} / {len(self._strings)} matching")
        else:
            self._summary_label.setText(f"{len(self._strings)} strings")
        # If the current selection is now hidden, jump to the first visible string row.
        cur = self._string_list.currentRow()
        if cur < 0 or self._string_list.isRowHidden(cur) or self._string_index_at_row(cur) is None:
            for row in range(self._string_list.count()):
                if not self._string_list.isRowHidden(row) and self._string_index_at_row(row) is not None:
                    self._string_list.setCurrentRow(row)
                    return
            self._set_current(None)

    # ---- event handlers ----------------------------------------------------

    def _on_string_changed(self, row: int) -> None:
        # Flush any in-flight edit on the previous selection before switching.
        self._commit_pending_edit()
        idx = self._string_index_at_row(row)
        if idx is None:
            self._set_current(None)
            return
        if self._session is not None and self._cursor_key is not None:
            self._session.remember_selection(self._cursor_key, idx)
        self._set_current(self._strings[idx])

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
        idx = self._string_index_at_row(row)
        if idx is not None:
            item = self._string_list.item(row)
            if item is not None:
                item.setText(self._row_label(idx, self._current))
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
        for idx, s in enumerate(self._strings):
            if s.offset == offset:
                self._reveal_string_index(idx)
                return

    def _on_jump_to_id(self) -> None:
        """Jump to the MSG.PAK string whose msg_id matches the box (decimal or
        0x-hex). No-op if the id isn't present."""
        txt = self._jump.text().strip()
        if not txt:
            return
        try:
            target = int(txt, 0)
        except ValueError:
            return
        for idx, s in enumerate(self._strings):
            if getattr(s, "msg_id", None) == target:
                self._reveal_string_index(idx)
                return

    def _reveal_string_index(self, idx: int) -> None:
        """Clear any active filter and scroll/select the row for model ``idx``."""
        row = self._row_of_string.get(idx)
        if row is None:
            return
        if self._string_list.isRowHidden(row):
            with QSignalBlocker(self._search):
                self._search.clear()
            self._apply_filter("")
        self._string_list.setCurrentRow(row)
        self._string_list.scrollToItem(self._string_list.item(row))


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
