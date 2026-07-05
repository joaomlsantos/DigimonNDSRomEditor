"""FarmTrainingPen editor — 48 fixed-stride 0x1C-byte records.

Each pen has four outcome chances (great-failure/failure/success/great-success)
whose bytes sum to `total_odds` (vanilla = 0x64 = 100). The four stat-delta
fields are signed s16 — vanilla uses 0xFFFF (-1) on great_failure to subtract a
point on a miss.
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QUndoStack
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from .cell_png_io import render_spr_index_qimage, spr_index_footprint
from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundSpinBox,
    OddsTotalLabel,
    _make_compact_grid,
    make_form,
    wrap_in_scroll,
)
from .record_list_panel import RecordListPanel


# SPR pak FAT paths — training-pen sprite_id indexes the same icon/portrait/UI
# sprite set the sprite browser edits.
_SPR_CHR = "DAT/SPR_CHR.PAK"
_SPR_PAL = "DAT/SPR_PAL.PAK"
_SPR_CEL = "DAT/SPR_CEL.PAK"
_SPRITE_PREVIEW_ZOOM = 3
# Pen sprites are all 32×32 in vanilla; anything else likely won't slot into
# the training-pen UI correctly.
_EXPECTED_SPRITE_SIZE = (32, 32)


def _pen_name(ix: int) -> str:
    if 0 <= ix < len(constants.FARM_TRAINING_PEN_NAMES):
        return constants.FARM_TRAINING_PEN_NAMES[ix]
    return f"<training pen {ix}>"


def _record_label(ix: int, _rec: model.FarmTrainingPen) -> str:
    return f"{ix:02d}  {_pen_name(ix)}"


class FarmTrainingPenEditor(QWidget):
    def __init__(
        self,
        records: List[model.FarmTrainingPen],
        undo_stack: QUndoStack,
        session=None,
        parent=None,
    ):
        super().__init__(parent)
        self._records = records
        self._undo_stack = undo_stack
        self._current_ix: int = -1
        self._all_widgets: List[object] = []
        self._sprite_preview: QLabel = None
        self._sprite_dim_note: QLabel = None
        # SPR paks power the sprite_id preview; absent when constructed without
        # a session (e.g. a bare test harness) — the preview just no-ops then.
        self._chr_pak = self._pal_pak = self._cel_pak = None
        if session is not None:
            try:
                self._chr_pak = session.sprite_pak(_SPR_CHR)
                self._pal_pak = session.sprite_pak(_SPR_PAL)
                self._cel_pak = session.sprite_pak(_SPR_CEL)
            except Exception:
                self._chr_pak = self._pal_pak = self._cel_pak = None

        self._list_panel = RecordListPanel(records, _record_label, dirty_aware=True)
        self._list_panel.indexSelected.connect(self._on_selection)

        self._detail = self._build_detail_container()

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._list_panel)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 740])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        undo_stack.indexChanged.connect(self._refresh_form)
        undo_stack.indexChanged.connect(self._list_panel.refresh_dirty_state)
        self._list_panel.select_first()

    def select_by_id(self, pen_id: int) -> bool:
        # Pens are identified by list index (id == index), so navigation from
        # the validation footer routes straight through select_index.
        return self._list_panel.select_index(pen_id)

    def _add_field(self, form, label: str, widget) -> None:
        form.addRow(label, widget)
        self._all_widgets.append(widget)

    def _build_detail_container(self) -> QWidget:
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(6, 6, 6, 6)
        cl.setSpacing(4)

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)
        cl.addWidget(self._title)

        # Empty session (e.g. DAWN_US until offsets are added) — show a stub
        # placeholder so the user gets a clear "nothing to edit here" instead
        # of a crash when the records list is empty.
        if not self._records:
            cl.addWidget(QLabel("No training-pen records loaded for this ROM version."))
            cl.addStretch(1)
            return wrap_in_scroll(content)

        first = self._records[0]

        identity = QGroupBox("Identity")
        identity_form = make_form(identity)
        self._add_field(identity_form, "String id",  BoundSpinBox(first, "string_id",  2, self._undo_stack, hex_display=True))
        self._add_field(identity_form, "Sprite id",  BoundSpinBox(first, "sprite_id",  2, self._undo_stack, hex_display=True))
        self._add_field(identity_form, "Stat op id", BoundSpinBox(first, "stat_op_id", 2, self._undo_stack, hex_display=True))

        # total_odds lives here (right under the four chances) rather than in a
        # separate Caps group so the stored total sits next to the odds it
        # totals; the computed-sum label flags any divergence from it.
        outcomes = QGroupBox("Outcome Values (signed stat delta) and Chances")
        outcomes_grid = _make_compact_grid(outcomes, cols=2)
        outcome_rows = [
            ("Great Failure", "great_failure_value", "great_failure_chance"),
            ("Failure",       "failure_value",       "failure_chance"),
            ("Success",       "success_value",       "success_chance"),
            ("Great Success", "great_success_value", "great_success_chance"),
        ]
        for row, (label, value_attr, chance_attr) in enumerate(outcome_rows):
            outcomes_grid.addWidget(QLabel(f"{label} value"), row, 0)
            v_spin = BoundSpinBox(first, value_attr, 2, self._undo_stack, signed=True)
            outcomes_grid.addWidget(v_spin, row, 1)
            self._all_widgets.append(v_spin)

            outcomes_grid.addWidget(QLabel(f"{label} chance"), row, 2)
            c_spin = BoundSpinBox(first, chance_attr, 1, self._undo_stack)
            outcomes_grid.addWidget(c_spin, row, 3)
            self._all_widgets.append(c_spin)

        total_row = len(outcome_rows)
        outcomes_grid.addWidget(QLabel("Total odds"), total_row, 2)
        total_spin = BoundSpinBox(first, "total_odds", 2, self._undo_stack)
        outcomes_grid.addWidget(total_spin, total_row, 3)
        self._all_widgets.append(total_spin)
        odds_total = OddsTotalLabel(
            first, [c for _, _, c in outcome_rows], "total_odds",
        )
        outcomes_grid.addWidget(odds_total, total_row + 1, 2, 1, 2)
        self._all_widgets.append(odds_total)

        caps = QGroupBox("Caps")
        caps_form = make_form(caps)
        self._add_field(caps_form, "Stat cap", BoundSpinBox(first, "stat_cap", 2, self._undo_stack))

        presentation = QGroupBox("Presentation")
        pres_form = make_form(presentation)
        self._add_field(pres_form, "Animation id",      BoundSpinBox(first, "animation_id",      2, self._undo_stack, hex_display=True))
        self._add_field(pres_form, "Sound id",          BoundSpinBox(first, "sound_id",          2, self._undo_stack, hex_display=True))
        self._add_field(pres_form, "Vertical position", BoundSpinBox(first, "vertical_position", 2, self._undo_stack))

        # Sprite preview sits right beside the Identity group (it visualises
        # Identity's sprite_id); the trailing stretch keeps it hugging the
        # inputs instead of drifting to the far right. The other groups flow
        # full-width below.
        identity_row = QHBoxLayout()
        identity_row.addWidget(identity)
        if self._chr_pak is not None:
            self._sprite_preview = QLabel()
            self._sprite_preview.setAlignment(Qt.AlignCenter)
            self._sprite_preview.setMinimumSize(96, 96)
            self._sprite_dim_note = QLabel()
            self._sprite_dim_note.setWordWrap(True)
            self._sprite_dim_note.setStyleSheet("color: #b00020; font-size: 11px;")
            self._sprite_dim_note.setVisible(False)
            preview_col = QWidget()
            pc = QVBoxLayout(preview_col)
            pc.setContentsMargins(0, 0, 0, 0)
            pc.addWidget(self._sprite_preview)
            pc.addWidget(self._sprite_dim_note)
            pc.addStretch(1)
            identity_row.addWidget(preview_col)
        identity_row.addStretch(1)

        cl.addLayout(identity_row)
        cl.addWidget(outcomes)
        cl.addWidget(caps)
        cl.addWidget(presentation)
        cl.addStretch(1)

        return wrap_in_scroll(content)

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._records)):
            return
        self._current_ix = ix
        target = self._records[ix]
        self._title.setText(
            f"{_pen_name(ix)}    (offset 0x{target.offset:08x})"
        )
        for w in self._all_widgets:
            w.rebind(target)
        self._update_sprite_preview()

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        for w in self._all_widgets:
            w.refresh()
        self._update_sprite_preview()

    def _update_sprite_preview(self) -> None:
        if self._sprite_preview is None:
            return
        if not (0 <= self._current_ix < len(self._records)):
            self._sprite_preview.clear()
            return
        sid = self._records[self._current_ix].sprite_id
        img = render_spr_index_qimage(
            self._chr_pak, self._pal_pak, self._cel_pak, sid,
        )
        if img is None or img.isNull():
            self._sprite_preview.setText(f"(no sprite 0x{sid:x})")
            self._sprite_dim_note.setVisible(False)
            return
        pm = QPixmap.fromImage(img)
        pm = pm.scaled(
            pm.width() * _SPRITE_PREVIEW_ZOOM, pm.height() * _SPRITE_PREVIEW_ZOOM,
            Qt.KeepAspectRatio, Qt.FastTransformation,
        )
        self._sprite_preview.setPixmap(pm)
        footprint = spr_index_footprint(self._cel_pak, sid)
        if footprint is not None and footprint != _EXPECTED_SPRITE_SIZE:
            w, h = footprint
            self._sprite_dim_note.setText(
                f"⚠ {w}×{h}, not 32×32 — may not display correctly."
            )
            self._sprite_dim_note.setVisible(True)
        else:
            self._sprite_dim_note.setVisible(False)


from .validation import ValidationIssue  # noqa: E402 — bottom-of-file utility


def farm_training_pen_issues(
    records: List[model.FarmTrainingPen],
    cel_pak=None,
) -> List[ValidationIssue]:
    """Footer-level issues for training pens (warning-only — nothing blocks
    saving):

    - the four outcome chances must sum to the stored ``total_odds``;
    - ``sprite_id`` should point at a 32×32 sprite (the pen UI's slot size) —
      checked only when ``cel_pak`` (SPR_CEL) is supplied.
    """
    issues: List[ValidationIssue] = []
    for ix, rec in enumerate(records):
        total = (
            rec.great_failure_chance + rec.failure_chance
            + rec.success_chance + rec.great_success_chance
        )
        if total != rec.total_odds:
            issues.append(ValidationIssue(
                section="Farm Training Pens",
                category="Odds Sum",
                message=(
                    f"{_pen_name(ix)} — chances sum to {total}, "
                    f"not the stored total odds ({rec.total_odds})."
                ),
                editor_key="farm_training_pens",
                record_id=ix,
            ))
        if cel_pak is not None:
            footprint = spr_index_footprint(cel_pak, rec.sprite_id)
            if footprint is not None and footprint != _EXPECTED_SPRITE_SIZE:
                w, h = footprint
                issues.append(ValidationIssue(
                    section="Farm Training Pens",
                    category="Sprite Dimensions",
                    message=(
                        f"{_pen_name(ix)} — sprite 0x{rec.sprite_id:x} is "
                        f"{w}×{h}, not 32×32; dimensions may not be compatible."
                    ),
                    editor_key="farm_training_pens",
                    record_id=ix,
                ))
    return issues
