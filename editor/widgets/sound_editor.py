"""Sound editor — vanilla BGM list + donor SDAT import audit + Replace.

Phase 1 (read-only): left pane lists every ``bgmNN`` slot in the ROM's
``dat/snd/sound_data.sdat`` with its SSEQ+SBNK+SWAR memory footprint.

Phase 2: right pane gains an ``Import SDAT…`` button that loads any
NDS donor SDAT and shows which of its BGMs survive trimming under the
166 KB per-BGM cap. Importable rows are styled normally; over-cap and
parse-failed rows are visible so the user can see what was rejected.

Phase 3 (this commit): ``Replace…`` builds a swap payload from the
selected donor row and stages it against the selected ROM slot via the
session's ``stage_bgm_swap`` channel (with QUndoCommand support).
``Revert`` drops a previously-staged swap on the selected ROM slot.
Staged slots are marked in the ROM list with a ``← donor ✱`` suffix;
the SDAT splice on ROM save lands in Phase 3d.

A two-column footer mirrors whichever rows are currently selected on
either side — ROM breakdown on the left, donor breakdown on the right
— matching the metadata-block pattern used by mchr_browser etc.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QSplitter,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from digimon_core.sound.donor import CAP_BYTES, DonorBgmRow, audit_donor
from digimon_core.sound.swap import build_swap

from editor.commands import (
    AddBgmCommand,
    RevertBgmAdditionCommand,
    RevertBgmSwapCommand,
    StageBgmSwapCommand,
)


def _fmt_kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


# Sort buckets for the donor "status" column: fits before over before parse-fail.
_STATUS_SORT_FITS = 0
_STATUS_SORT_OVER = 1
_STATUS_SORT_PARSE_FAIL = 2


def _donor_status_sort_key(row: DonorBgmRow) -> int:
    if row.parse_failed:
        return _STATUS_SORT_PARSE_FAIL
    if row.fits_cap:
        return _STATUS_SORT_FITS
    return _STATUS_SORT_OVER


class _DonorRowItem(QTreeWidgetItem):
    """Tree item that sorts Size numerically, Status by bucket, Name by text.

    We handle all three columns ourselves rather than calling
    ``super().__lt__()`` for the Name case — PySide6's binding around
    QTreeWidgetItem's base ``operator<`` segfaults during the Qt sort
    machinery if any item in the model isn't a known C++ subclass.
    """

    def __init__(self, donor_idx: int, row: DonorBgmRow):
        super().__init__()
        self._donor_idx = donor_idx
        self._name_key = row.name.casefold()
        self._size_key = row.trimmed_total
        self._status_key = _donor_status_sort_key(row)
        self.setData(0, Qt.UserRole, donor_idx)

    def __lt__(self, other) -> bool:  # type: ignore[override]
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else 0
        if isinstance(other, _DonorRowItem):
            if col == 1:
                return self._size_key < other._size_key
            if col == 2:
                return self._status_key < other._status_key
            return self._name_key < other._name_key
        # Mixed-type compare (shouldn't happen in this tree); fall back to text.
        return self.text(col) < other.text(col)


def _cap_note(total_bytes: int) -> str:
    cap_kb = CAP_BYTES / 1024
    used_kb = total_bytes / 1024
    if total_bytes > CAP_BYTES:
        return (
            f"{used_kb:.1f} KB \u2014 over the {cap_kb:.0f} KB cap by "
            f"{used_kb - cap_kb:.1f} KB"
        )
    return (
        f"{used_kb:.1f} KB \u2014 "
        f"{cap_kb - used_kb:.1f} KB headroom under the {cap_kb:.0f} KB cap"
    )


class SoundEditor(QWidget):
    """Browse vanilla ROM BGMs + audit a donor SDAT for importable slots."""

    def __init__(self, session, undo_stack=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._bgms = session.vanilla_bgm_summary()

        # Donor pane state mirrors a session-level cache so it survives
        # editor teardown when the user navigates away and back. Rehydrate
        # from the session if a donor was imported earlier this session.
        cached = session.sound_donor()
        if cached is not None:
            path, data, rows = cached
            self._donor_path: Optional[Path] = Path(path)
            self._donor_bytes: Optional[bytes] = data
            self._donor_rows: List[DonorBgmRow] = list(rows)
        else:
            self._donor_path = None
            self._donor_bytes = None
            self._donor_rows = []

        self._build_ui()
        # _rebuild_rom_list selects row 0 → fires _on_rom_row_changed which
        # references footer widgets, so it has to run after _build_footer.
        self._rebuild_rom_list()
        if self._donor_rows:
            self._populate_donor_list()
        self._refresh_action_state()

    # ---- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_pane())
        splitter.addWidget(self._build_right_pane())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 700])
        outer.addWidget(splitter, 1)

        outer.addWidget(self._build_footer())

    def _build_rom_actions(self) -> QWidget:
        """Action bar that lives at the bottom of the ROM (left) pane."""
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(8)

        self._revert_btn = QToolButton()
        self._revert_btn.setText("Revert")
        self._revert_btn.setToolTip(
            "If a vanilla slot is selected: drop its staged donor swap and "
            "restore the vanilla BGM. If a pending new entry is selected: "
            "remove it from the additions list."
        )
        self._revert_btn.clicked.connect(self._on_revert_clicked)
        row.addWidget(self._revert_btn)

        row.addStretch(1)
        return bar

    def _build_donor_actions(self) -> QWidget:
        """Action bar that lives at the bottom of the donor (right) pane."""
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(8)

        self._import_btn = QToolButton()
        self._import_btn.setText("Import SDAT\u2026")
        self._import_btn.setToolTip(
            "Load a donor SDAT (from another NDS game) and audit which BGMs "
            "fit DWDD's per-BGM memory cap."
        )
        self._import_btn.clicked.connect(self._on_import_clicked)
        row.addWidget(self._import_btn)

        self._replace_btn = QToolButton()
        self._replace_btn.setText("Replace\u2026")
        self._replace_btn.setToolTip(
            "Stage the selected donor BGM into the selected ROM slot. Takes "
            "effect on the next ROM save."
        )
        self._replace_btn.clicked.connect(self._on_replace_clicked)
        row.addWidget(self._replace_btn)

        self._add_btn = QToolButton()
        self._add_btn.setText("Add As New Entry")
        self._add_btn.setToolTip(
            "Append the selected donor BGM as a brand-new SDAT slot. The new "
            "entry gets the next sequential bgm id (e.g. bgm35 for vanilla "
            "DWDD). Useful as a staging tool while you find the script "
            "opcodes that trigger the new id."
        )
        self._add_btn.clicked.connect(self._on_add_as_new_clicked)
        row.addWidget(self._add_btn)

        row.addStretch(1)
        return bar

    def _build_left_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        header = QLabel(f"ROM BGMs \u2014 {len(self._bgms)} slots")
        header.setStyleSheet("color: #888;")
        layout.addWidget(header)

        self._rom_list = QListWidget()
        self._rom_list.setFont(QFont("Consolas", 10))
        self._rom_list.setUniformItemSizes(True)
        self._rom_list.currentRowChanged.connect(self._on_rom_row_changed)
        layout.addWidget(self._rom_list, 1)

        self._rom_header = header

        layout.addWidget(self._build_rom_actions())
        return pane

    def _build_right_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self._donor_header = QLabel("Donor SDAT \u2014 click Import SDAT\u2026")
        self._donor_header.setStyleSheet("color: #888;")
        layout.addWidget(self._donor_header)

        self._donor_list = QTreeWidget()
        self._donor_list.setFont(QFont("Consolas", 10))
        self._donor_list.setRootIsDecorated(False)
        self._donor_list.setUniformRowHeights(True)
        self._donor_list.setAllColumnsShowFocus(True)
        self._donor_list.setColumnCount(3)
        self._donor_list.setHeaderLabels(["Name", "Size", ""])
        header = self._donor_list.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        self._donor_list.setSortingEnabled(True)
        self._donor_list.sortByColumn(1, Qt.AscendingOrder)
        self._donor_list.currentItemChanged.connect(
            lambda cur, _prev: self._on_donor_row_changed(cur)
        )
        layout.addWidget(self._donor_list, 1)

        self._donor_status = QLabel("")
        self._donor_status.setStyleSheet("color: #888;")
        self._donor_status.setWordWrap(True)
        layout.addWidget(self._donor_status)

        layout.addWidget(self._build_donor_actions())
        return pane

    def _build_footer(self) -> QWidget:
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)

        wrap = QWidget()
        wrap_layout = QVBoxLayout(wrap)
        wrap_layout.setContentsMargins(0, 0, 0, 0)
        wrap_layout.setSpacing(0)
        wrap_layout.addWidget(sep)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(8, 6, 8, 6)
        body_layout.setSpacing(24)
        body_layout.addWidget(self._build_rom_detail(), 1)

        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setFrameShadow(QFrame.Sunken)
        body_layout.addWidget(divider)

        body_layout.addWidget(self._build_donor_detail(), 1)
        wrap_layout.addWidget(body)
        return wrap

    def _build_rom_detail(self) -> QWidget:
        col = QWidget()
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._rom_detail_title = QLabel("ROM BGM \u2014 \u2014")
        tf = self._rom_detail_title.font()
        tf.setBold(True)
        self._rom_detail_title.setFont(tf)
        layout.addWidget(self._rom_detail_title)

        self._rom_detail_cap = QLabel("\u2014")
        self._rom_detail_cap.setStyleSheet("color: #888;")
        layout.addWidget(self._rom_detail_cap)

        self._rom_meta_sseq = QLabel("\u2014")
        self._rom_meta_sbnk = QLabel("\u2014")
        self._rom_meta_swar = QLabel("\u2014")
        self._rom_meta_slots = QLabel("\u2014")
        self._rom_meta_slots.setWordWrap(True)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(2)
        form.addRow("SSEQ", self._rom_meta_sseq)
        form.addRow("SBNK", self._rom_meta_sbnk)
        form.addRow("waveArc", self._rom_meta_swar)
        form.addRow("Slots", self._rom_meta_slots)
        layout.addLayout(form)
        layout.addStretch(1)
        return col

    def _build_donor_detail(self) -> QWidget:
        col = QWidget()
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._donor_detail_title = QLabel("Donor BGM \u2014 \u2014")
        tf = self._donor_detail_title.font()
        tf.setBold(True)
        self._donor_detail_title.setFont(tf)
        layout.addWidget(self._donor_detail_title)

        self._donor_detail_cap = QLabel("\u2014")
        self._donor_detail_cap.setStyleSheet("color: #888;")
        layout.addWidget(self._donor_detail_cap)

        self._donor_meta_sseq = QLabel("\u2014")
        self._donor_meta_sbnk = QLabel("\u2014")
        self._donor_meta_swar = QLabel("\u2014")
        self._donor_meta_progs = QLabel("\u2014")

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(2)
        form.addRow("SSEQ", self._donor_meta_sseq)
        form.addRow("SBNK", self._donor_meta_sbnk)
        form.addRow("waveArc", self._donor_meta_swar)
        form.addRow("Programs kept", self._donor_meta_progs)
        layout.addLayout(form)
        layout.addStretch(1)
        return col

    # ---- selection -> footer --------------------------------------------

    def _on_rom_row_changed(self, row: int) -> None:
        kind, payload = self._rom_row_kind(row)
        if kind is None:
            self._rom_detail_title.setText("ROM BGM \u2014 \u2014")
            self._rom_detail_cap.setText("\u2014")
            for lbl in (
                self._rom_meta_sseq, self._rom_meta_sbnk,
                self._rom_meta_swar, self._rom_meta_slots,
            ):
                lbl.setText("\u2014")
            self._refresh_action_state()
            return

        if kind == "vanilla":
            bgm = payload
            self._rom_detail_title.setText(f"ROM BGM \u2014 {bgm.seq_name}")
            staged = self._session.staged_bgm_swap(bgm.bgm_id)
            if staged is not None:
                self._rom_detail_cap.setText(
                    f"Staged: {staged.donor_label or '?'} \u2014 "
                    f"{_cap_note(staged.trimmed_total_bytes)}"
                )
            else:
                self._rom_detail_cap.setText(_cap_note(bgm.total_size))
            self._rom_meta_sseq.setText(_fmt_kb(bgm.sseq_size))
            self._rom_meta_sbnk.setText(
                f"{bgm.bank_name} ({_fmt_kb(bgm.sbnk_size)})"
            )
            self._rom_meta_swar.setText(
                f"{_fmt_kb(bgm.swar_total_size)} across "
                f"{len(bgm.wave_names)} slot(s)"
            )
            self._rom_meta_slots.setText(
                ", ".join(bgm.wave_names) if bgm.wave_names else "(none)"
            )
        else:  # "addition"
            addition_idx, swap = payload
            new_bgm_id = len(self._bgms) + addition_idx
            self._rom_detail_title.setText(
                f"ROM BGM \u2014 bgm{new_bgm_id:02d} (pending new)"
            )
            self._rom_detail_cap.setText(
                f"New entry from {swap.donor_label or '?'} \u2014 "
                f"{_cap_note(swap.trimmed_total_bytes)}"
            )
            self._rom_meta_sseq.setText(_fmt_kb(len(swap.sseq_bytes)))
            self._rom_meta_sbnk.setText(
                f"BANK_BGM{new_bgm_id:02d} ({_fmt_kb(len(swap.sbnk_bytes))})"
            )
            self._rom_meta_swar.setText(
                f"{_fmt_kb(len(swap.swar_bytes))} (single-slot merged)"
            )
            self._rom_meta_slots.setText(f"WAVE_BGM{new_bgm_id:02d}")
        self._refresh_action_state()

    def _on_donor_row_changed(self, current=None) -> None:
        if isinstance(current, QTreeWidgetItem):
            data = current.data(0, Qt.UserRole)
            row = int(data) if data is not None else -1
        elif isinstance(current, int):
            row = current
        else:
            row = -1
        if row < 0 or row >= len(self._donor_rows):
            self._donor_detail_title.setText("Donor BGM \u2014 \u2014")
            self._donor_detail_cap.setText("\u2014")
            for lbl in (
                self._donor_meta_sseq, self._donor_meta_sbnk,
                self._donor_meta_swar, self._donor_meta_progs,
            ):
                lbl.setText("\u2014")
            self._refresh_action_state()
            return

        r = self._donor_rows[row]
        self._donor_detail_title.setText(f"Donor BGM \u2014 {r.name}")

        if r.parse_failed:
            self._donor_detail_cap.setText(
                "SSEQ or SBNK didn't walk cleanly \u2014 unimportable"
            )
        else:
            self._donor_detail_cap.setText(_cap_note(r.trimmed_total))

        self._donor_meta_sseq.setText(_fmt_kb(r.sseq_size))
        self._donor_meta_sbnk.setText(
            f"{r.bank_name} ({_fmt_kb(r.sbnk_size)})" if r.bank_name
            else _fmt_kb(r.sbnk_size)
        )
        if r.parse_failed:
            self._donor_meta_swar.setText("\u2014")
            self._donor_meta_progs.setText("\u2014")
        else:
            self._donor_meta_swar.setText(
                f"{_fmt_kb(r.swar_trimmed_total)} trimmed "
                f"(orig {_fmt_kb(r.swar_original_total)}) "
                f"across {r.slots_used} slot(s)"
            )
            self._donor_meta_progs.setText(str(r.progs_used))
        self._refresh_action_state()

    # ---- import flow ----------------------------------------------------

    def _on_import_clicked(self) -> None:
        start_dir = str(self._donor_path.parent) if self._donor_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import donor SDAT",
            start_dir,
            "NDS sound archives (*.sdat);;All files (*)",
        )
        if not path:
            return

        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            QMessageBox.warning(self, "Import SDAT", f"Could not read file:\n{exc}")
            return

        try:
            rows = audit_donor(data)
        except ValueError as exc:
            QMessageBox.warning(self, "Import SDAT", f"Not a valid SDAT:\n{exc}")
            return

        self._donor_path = Path(path)
        self._donor_bytes = data
        self._donor_rows = rows
        self._session.set_sound_donor(path, data, rows)
        self._populate_donor_list()

    def _populate_donor_list(self) -> None:
        # Disable sorting while we batch-add rows so the items end up at
        # the indices we wrote them to; re-enable below.
        self._donor_list.setSortingEnabled(False)
        self._donor_list.clear()
        # Resetting the model fires currentItemChanged(None,…) which clears
        # the footer; that's the right thing while we wait for the user to pick.
        self._on_donor_row_changed(None)

        if not self._donor_rows:
            assert self._donor_path is not None
            self._donor_header.setText(
                f"Donor: {self._donor_path.name} \u2014 no sequences found"
            )
            self._donor_status.setText("")
            self._donor_list.setSortingEnabled(True)
            return

        assert self._donor_path is not None
        n_fit = sum(1 for r in self._donor_rows if r.fits_cap)
        n_parse_fail = sum(1 for r in self._donor_rows if r.parse_failed)
        self._donor_header.setText(
            f"Donor: {self._donor_path.name} \u2014 "
            f"{n_fit}/{len(self._donor_rows)} BGMs fit 166 KB cap"
        )

        cap_kb = CAP_BYTES / 1024
        first_selectable_item: Optional[QTreeWidgetItem] = None
        for idx, row in enumerate(self._donor_rows):
            item = _DonorRowItem(idx, row)
            item.setText(0, row.name)
            item.setText(1, _fmt_kb(row.trimmed_total))
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            if row.parse_failed:
                item.setText(2, "\u2717")
                item.setToolTip(
                    2,
                    "SSEQ or SBNK didn't walk cleanly \u2014 this sequence is "
                    "unimportable.",
                )
            elif row.fits_cap:
                item.setText(2, "\u2713")
            else:
                over_kb = (row.trimmed_total - CAP_BYTES) / 1024
                item.setText(2, "\u26a0")
                over_msg = (
                    f"Over the {cap_kb:.0f} KB cap by {over_kb:.1f} KB \u2014 "
                    "pick a smaller donor."
                )
                item.setToolTip(1, over_msg)
                item.setToolTip(2, over_msg)
            item.setTextAlignment(2, Qt.AlignCenter)
            self._donor_list.addTopLevelItem(item)
            if first_selectable_item is None and not row.parse_failed:
                first_selectable_item = item

        self._donor_list.setSortingEnabled(True)
        # Re-apply the user's current sort indicator after the batch insert.
        header = self._donor_list.header()
        self._donor_list.sortByColumn(
            header.sortIndicatorSection(), header.sortIndicatorOrder()
        )

        bits = [
            f"Per-row total = SSEQ + trimmed SBNK + \u03a3 trimmed SWAR "
            f"(cap {cap_kb:.0f} KB)."
        ]
        if n_parse_fail:
            bits.append(
                f"{n_parse_fail} sequence(s) failed to walk \u2014 likely a "
                "non-NDS SSEQ dialect; skip for now."
            )
        self._donor_status.setText(" ".join(bits))

        if first_selectable_item is not None:
            self._donor_list.setCurrentItem(first_selectable_item)

    # ---- replace / revert ------------------------------------------------

    def _selected_rom_bgm(self):
        """Return the currently-selected VANILLA BgmSummary (or None)."""
        kind, payload = self._rom_row_kind(self._rom_list.currentRow())
        if kind != "vanilla":
            return None
        return payload

    def _selected_donor_row(self) -> Optional[DonorBgmRow]:
        item = self._donor_list.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.UserRole)
        if data is None:
            return None
        idx = int(data)
        if idx < 0 or idx >= len(self._donor_rows):
            return None
        return self._donor_rows[idx]

    def _refresh_action_state(self) -> None:
        """Enable Replace/Revert/Add based on the current ROM + donor picks."""
        kind, payload = self._rom_row_kind(self._rom_list.currentRow())
        donor = self._selected_donor_row()

        donor_importable = (
            donor is not None and not donor.parse_failed and donor.fits_cap
            and self._donor_bytes is not None
        )

        # Replace targets a VANILLA slot (additions get removed instead).
        can_replace = (
            self._undo_stack is not None
            and kind == "vanilla"
            and donor_importable
        )
        self._replace_btn.setEnabled(can_replace)

        # Add appends a new slot; only the donor pick matters.
        self._add_btn.setEnabled(
            self._undo_stack is not None and donor_importable
        )

        # Revert: drops a staged swap (vanilla row) or a pending addition.
        if self._undo_stack is None:
            self._revert_btn.setEnabled(False)
        elif kind == "vanilla":
            staged = self._session.staged_bgm_swap(payload.bgm_id)
            self._revert_btn.setEnabled(staged is not None)
        elif kind == "addition":
            self._revert_btn.setEnabled(True)
        else:
            self._revert_btn.setEnabled(False)

    def _on_replace_clicked(self) -> None:
        bgm = self._selected_rom_bgm()
        donor = self._selected_donor_row()
        if bgm is None or donor is None or self._donor_bytes is None:
            return
        if donor.parse_failed or not donor.fits_cap:
            return
        donor_game = self._donor_path.stem if self._donor_path else ""
        try:
            swap = build_swap(
                self._donor_bytes,
                donor.seq_index,
                target_bgm_id=bgm.bgm_id,
                donor_label=donor.name,
                donor_game_label=donor_game,
            )
        except ValueError as exc:
            QMessageBox.warning(
                self,
                "Replace BGM",
                f"Couldn't build a swap payload for {donor.name!r}:\n{exc}",
            )
            return
        if swap.trimmed_total_bytes > CAP_BYTES:
            # build_swap can come in a few bytes over the audit's per-slot
            # trimmed estimate if the flatten step lands less efficiently
            # than per-slot SWARs; refuse rather than corrupt the slot.
            QMessageBox.warning(
                self,
                "Replace BGM",
                f"Built payload is {_fmt_kb(swap.trimmed_total_bytes)} \u2014 "
                f"over the {_fmt_kb(CAP_BYTES)} cap. Pick a smaller donor.",
            )
            return

        cmd = StageBgmSwapCommand(
            self._session,
            swap,
            description=f"Replace {bgm.seq_name} with {donor.name}",
            on_change=self._on_staged_swaps_changed,
        )
        self._undo_stack.push(cmd)

    def _on_revert_clicked(self) -> None:
        kind, payload = self._rom_row_kind(self._rom_list.currentRow())
        if kind == "vanilla":
            bgm = payload
            if self._session.staged_bgm_swap(bgm.bgm_id) is None:
                return
            cmd = RevertBgmSwapCommand(
                self._session,
                bgm.bgm_id,
                description=f"Revert {bgm.seq_name} to vanilla",
                on_change=self._on_staged_swaps_changed,
            )
            self._undo_stack.push(cmd)
        elif kind == "addition":
            addition_idx, swap = payload
            new_bgm_id = len(self._bgms) + addition_idx
            cmd = RevertBgmAdditionCommand(
                self._session,
                addition_idx,
                description=(
                    f"Remove new bgm{new_bgm_id:02d} "
                    f"({swap.donor_label or 'donor'})"
                ),
                on_change=self._on_staged_swaps_changed,
            )
            self._undo_stack.push(cmd)

    def _on_add_as_new_clicked(self) -> None:
        donor = self._selected_donor_row()
        if donor is None or self._donor_bytes is None:
            return
        if donor.parse_failed or not donor.fits_cap:
            return
        donor_game = self._donor_path.stem if self._donor_path else ""
        new_bgm_id = len(self._bgms) + len(self._session.staged_bgm_additions())
        try:
            swap = build_swap(
                self._donor_bytes,
                donor.seq_index,
                target_bgm_id=-1,  # sentinel: additions don't address a vanilla slot
                donor_label=donor.name,
                donor_game_label=donor_game,
            )
        except ValueError as exc:
            QMessageBox.warning(
                self,
                "Add BGM",
                f"Couldn't build a swap payload for {donor.name!r}:\n{exc}",
            )
            return
        if swap.trimmed_total_bytes > CAP_BYTES:
            QMessageBox.warning(
                self,
                "Add BGM",
                f"Built payload is {_fmt_kb(swap.trimmed_total_bytes)} \u2014 "
                f"over the {_fmt_kb(CAP_BYTES)} cap. Pick a smaller donor.",
            )
            return

        cmd = AddBgmCommand(
            self._session,
            swap,
            description=f"Add bgm{new_bgm_id:02d} from {donor.name}",
            on_change=self._on_staged_swaps_changed,
        )
        self._undo_stack.push(cmd)

    def _on_staged_swaps_changed(self) -> None:
        """Repaint ROM rows + footer + action state after a stage/add flip.

        Additions can grow or shrink the list, so we rebuild rather than
        just relabel — keeps the row indices in sync with
        ``_staged_bgm_additions``.
        """
        self._rebuild_rom_list()
        self._on_rom_row_changed(self._rom_list.currentRow())

    def _rebuild_rom_list(self) -> None:
        """Repaint the ROM list with vanilla bgms + staged additions appended."""
        prev_row = self._rom_list.currentRow() if self._rom_list.count() else 0
        self._rom_list.blockSignals(True)
        self._rom_list.clear()
        for bgm in self._bgms:
            self._rom_list.addItem(QListWidgetItem(self._rom_list_label(bgm)))
        additions = self._session.staged_bgm_additions()
        vanilla_count = len(self._bgms)
        for i, swap in enumerate(additions):
            self._rom_list.addItem(
                QListWidgetItem(self._addition_list_label(vanilla_count + i, swap))
            )
        new_count = self._rom_list.count()
        self._rom_header.setText(
            f"ROM BGMs \u2014 {len(self._bgms)} vanilla"
            + (f" + {len(additions)} new" if additions else "")
        )
        self._rom_list.blockSignals(False)
        if new_count > 0:
            self._rom_list.setCurrentRow(min(prev_row, new_count - 1))

    def _rom_list_label(self, bgm) -> str:
        kb = _fmt_kb(bgm.total_size)
        warn = " \u26a0" if bgm.total_size > CAP_BYTES else ""
        staged = self._session.staged_bgm_swap(bgm.bgm_id)
        if staged is None:
            return f"{bgm.seq_name:8s}{kb:>10s}{warn}"
        donor = staged.donor_label or "?"
        staged_kb = _fmt_kb(staged.trimmed_total_bytes)
        return (
            f"{bgm.seq_name:8s}{kb:>10s}{warn}  \u2190 {donor} "
            f"({staged_kb}) \u2731"
        )

    def _addition_list_label(self, new_bgm_id: int, swap) -> str:
        name = f"bgm{new_bgm_id:02d}"
        kb = _fmt_kb(swap.trimmed_total_bytes)
        donor = swap.donor_label or "?"
        return f"{name:8s}{kb:>10s}  \u2728 new \u2190 {donor}"

    # ---- ROM row \u2192 (bgm | addition) classification ------------------------

    def _rom_row_kind(self, row: int):
        """Return ('vanilla', bgm) | ('addition', (idx, swap)) | (None, None)."""
        if row < 0:
            return (None, None)
        if row < len(self._bgms):
            return ("vanilla", self._bgms[row])
        addition_idx = row - len(self._bgms)
        additions = self._session.staged_bgm_additions()
        if addition_idx < len(additions):
            return ("addition", (addition_idx, additions[addition_idx]))
        return (None, None)

    # ---- lifecycle -------------------------------------------------------

    def aboutToTeardown(self) -> None:
        # No pooled widgets to release; donor SDAT bytes drop when this
        # widget is GC'd (next session open or editor close).
        pass
