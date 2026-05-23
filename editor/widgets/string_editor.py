"""String editor — browse and edit in-game text for one region.

Each region (msgpak block, arm9 block, overlay block) is a packed run of
strings split on [END] (FE FF) or FF FF. Pointers to individual strings
live elsewhere in the ROM and aren't repointed by the editor, so each
string's encoded length must stay within its original byte budget
(parse-time `original_byte_length`). When shortened, the terminator is
rewritten as [END] so the engine stops cleanly at the new boundary.
"""
from __future__ import annotations

from typing import List, Optional

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
    flat = text.replace("[BR]", " ").replace("[END]", " / ")
    if len(flat) > _PREVIEW_MAX:
        return flat[:_PREVIEW_MAX - 1] + "…"
    return flat


class StringEditor(QWidget):
    """Two-pane editor for one region: string list | text + budget meter."""

    def __init__(
        self,
        strings_in_region: List[model.GameString],
        undo_stack: QUndoStack,
    ):
        super().__init__()
        self._strings = strings_in_region
        self._undo_stack = undo_stack
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
        self._offset_label.setText(
            f"Offset 0x{s.offset:08X} — budget {s.original_byte_length} bytes"
        )
        with QSignalBlocker(self._text_edit):
            self._text_edit.setPlainText(s.text)
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
        new_text = self._text_edit.toPlainText()
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
        if self._text_edit.toPlainText() != self._current.text:
            with QSignalBlocker(self._text_edit):
                self._text_edit.setPlainText(self._current.text)
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
        """Update the budget meter from a raw text value (no model lookup)."""
        if self._current is None:
            self._budget_label.setText("")
            return
        budget = self._current.original_byte_length
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
