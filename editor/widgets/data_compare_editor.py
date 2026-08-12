"""Inline compare / swap / copy companion for the base + enemy data editors.

``CompareSwapPanel`` is the "B" side that the base / enemy editors reveal beside
their own form via a ``⇄ Compare / Swap`` toggle. "A" is whatever the editor is
currently editing (supplied by a getter); the panel picks a "B" record, shows it
as a reflowing :class:`DigimonCompareForm`, and offers Swap / Copy A→B / Copy
B→A — enabled only when A and B are the same type and two distinct records. Each
op exchanges / copies the whole record except the internal id (per the model
contract) as one undoable command on the editor's shared stack.

``DigimonCompareForm`` is self-contained rather than the editor's own detail
widget: the editor's form owns a single-instance combo / sprite-picker pool, so
a second copy can't reuse it. The compare form omits editor-only context (sprite
row, growth / expected-stats sidecars, cross-ref lists) and builds its move /
trait combos fresh from the session-shared picker models (``shared_kind=``) —
cheap and pool-free.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from ..commands import (
    CopyDigimonRecordCommand,
    CopyDisplayCommand,
    SwapDigimonRecordCommand,
    SwapDisplayCommand,
)
from .flow_layout import FlowLayout, make_height_for_width
from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundBitCheckBox,
    BoundCheckBox,
    BoundEnumCombo,
    BoundIdCombo,
    BoundSpinBox,
    add_unknown_form_row,
    make_form,
    move_choices,
    stat_cell,
    trait_choices,
    wrap_in_scroll,
)

# Field groupings reused verbatim from the editors so the compare view stays in
# sync with whatever the editors expose.
from .base_digimon_editor import (
    _AFFINITY_FIELDS as _BASE_AFFINITY,
    _MISC_FIELDS as _BASE_MISC,
    _MOVE_FIELDS as _MOVE_FIELDS,
    _RES_FIELDS as _RES_FIELDS,
    _STAT_FIELDS as _BASE_STATS,
    _TRAIT_FIELDS as _BASE_TRAITS,
)
from .enemy_digimon_editor import (
    scripted_area_label,
    _AFFINITY_FIELDS as _ENEMY_AFFINITY,
    _EXP_FIELDS as _ENEMY_EXP,
    _MISC_FIELDS as _ENEMY_MISC,
    _STAT_FIELDS as _ENEMY_STATS,
    _TRAIT_FIELDS as _ENEMY_TRAITS,
    _USAGE_WEIGHT_FIELDS as _ENEMY_WEIGHTS,
)


def _short(name: Optional[str], limit: int = 12) -> str:
    """Truncate a digimon name for button labels — long names (e.g.
    ``Imperialdramon Dragon Mode(Black)``) otherwise bloat the buttons."""
    if not name:
        return "?"
    return name if len(name) <= limit else name[: limit - 1] + "…"


class DigimonCompareForm(QWidget):
    """Compact, editable, reflowing view of one base/enemy record's data.

    Each stat / resistance / element / exp / weight is a wrapping cell; traits
    and moves are combo rows. Bound widgets are tracked so :meth:`refresh` can
    re-read the model after a swap / copy / undo (which re-parse the record in
    place, keeping the same object the widgets are bound to).
    """

    def __init__(self, record, is_enemy: bool, undo_stack: Optional[QUndoStack],
                 session, parent=None):
        super().__init__(parent)
        self._record = record
        self._is_enemy = is_enemy
        self._undo = undo_stack
        self._session = session
        self._bound: List[object] = []  # widgets exposing refresh()
        self._display_slot = None
        self._disp_previews: dict = {}   # attr -> (QLabel, render_fn)
        self._build()

    # ---- refresh ---------------------------------------------------------

    def refresh(self) -> None:
        for w in self._bound:
            try:
                w.refresh()
            except RuntimeError:
                pass  # widget deleted
        if self._disp_previews:
            self._refresh_display_previews()

    def _track(self, w):
        self._bound.append(w)
        return w

    # ---- construction helpers -------------------------------------------

    def _cell_flow_box(self, title: str) -> Tuple[QWidget, FlowLayout]:
        box = QGroupBox(title)
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)
        wrap = QWidget()
        flow = FlowLayout(wrap, margin=0, h_spacing=12, v_spacing=6)
        make_height_for_width(wrap)
        outer.addWidget(wrap)
        make_height_for_width(box)
        return flow, box

    def _spin_cell(self, flow: FlowLayout, attr: str, label: str, width: int) -> None:
        spin = self._track(BoundSpinBox(self._record, attr, width, self._undo))
        spin.setAlignment(Qt.AlignCenter)
        flow.addWidget(stat_cell(label, spin))

    # ---- build -----------------------------------------------------------

    def _build(self) -> None:
        rec = self._record
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Identity — id is read-only (swap/copy preserve it); the rest editable.
        id_box = QGroupBox("Identity")
        id_form = make_form(id_box)
        id_form.addRow("ID", BoundSpinBox(
            rec, "id", 2, self._undo, hex_display=True, read_only=True))
        id_form.addRow("Species", self._track(
            BoundEnumCombo(rec, "species", model.Species, self._undo)))
        if not self._is_enemy:
            id_form.addRow("StatType", self._track(
                BoundEnumCombo(rec, "digimon_type", model.DigimonType, self._undo)))
            id_form.addRow("Scannable", self._track(
                BoundCheckBox(rec, "is_scannable", self._undo)))
        root.addWidget(id_box)

        # Display fields (sprite_map + battle-string) — swap/copy carry these
        # too, so they're shown here for reference + direct editing.
        display_box = self._build_display_box()
        if display_box is not None:
            root.addWidget(display_box)

        # Stats (+ Level) as wrapping cells.
        flow, stats_box = self._cell_flow_box("Stats")
        self._spin_cell(flow, "level", "Level", 1)
        stats = _ENEMY_STATS if self._is_enemy else [(a, l) for a, l, *_ in _BASE_STATS]
        for attr, label in stats:
            self._spin_cell(flow, attr, label, 2)
        root.addWidget(stats_box)

        flow, res_box = self._cell_flow_box("Resistances")
        for attr, label in _RES_FIELDS:
            self._spin_cell(flow, attr, label, 2)
        root.addWidget(res_box)

        flow, aff_box = self._cell_flow_box("Element Affinity")
        affinity = _ENEMY_AFFINITY if self._is_enemy else _BASE_AFFINITY
        for bit, label in affinity:
            check = self._track(BoundBitCheckBox(rec, "element_affinity", bit, self._undo))
            flow.addWidget(stat_cell(label, check))
        root.addWidget(aff_box)

        # Traits + Moves as combo forms (fresh combos over the shared models).
        traits = _ENEMY_TRAITS if self._is_enemy else _BASE_TRAITS
        trait_none = 0xFFFF if self._is_enemy else 0xFF
        trait_kind = "traits_word" if self._is_enemy else "traits_byte"
        traits_box = QGroupBox("Traits")
        traits_form = make_form(traits_box)
        for attr, label in traits:
            traits_form.addRow(label, self._track(BoundIdCombo(
                rec, attr, trait_choices(), self._undo,
                none_value=trait_none, none_label="(none)", shared_kind=trait_kind)))
        root.addWidget(traits_box)

        moves_box = QGroupBox("Moves")
        moves_form = make_form(moves_box)
        for attr, label in _MOVE_FIELDS:
            moves_form.addRow(label, self._track(BoundIdCombo(
                rec, attr, move_choices(), self._undo,
                none_value=0xFFFF, none_label="(none)",
                details_kind="move", shared_kind="moves")))
        root.addWidget(moves_box)

        if self._is_enemy:
            flow, exp_box = self._cell_flow_box("Exp Yield by Tamer Species")
            for attr, label in _ENEMY_EXP:
                self._spin_cell(flow, attr, label, 4)
            root.addWidget(exp_box)
            flow, wt_box = self._cell_flow_box("Move Usage")
            for attr, label in _ENEMY_WEIGHTS:
                self._spin_cell(flow, attr, label, 1)
            root.addWidget(wt_box)

        misc = _ENEMY_MISC if self._is_enemy else _BASE_MISC
        misc_box = QGroupBox("Misc")
        misc_form = make_form(misc_box)
        for field in misc:
            attr, label, width, hex_disp = field[0], field[1], field[2], field[3]
            spin = self._track(
                BoundSpinBox(rec, attr, width, self._undo, hex_display=hex_disp))
            if attr.startswith("unknown_"):
                # Honour the global "show unknown fields" toggle, same as the
                # editors — otherwise unknowns show here regardless.
                add_unknown_form_row(misc_form, label, spin)
            else:
                misc_form.addRow(label, spin)
        root.addWidget(misc_box)

        root.addStretch(1)

    def _build_display_box(self) -> Optional[QWidget]:
        """Sprite-map + battle-string 'display' fields for this record's id.

        Fresh, non-pooled pickers over the session-shared sprite models, so
        they don't fight the editor's pooled sprite row. ``None`` when the id
        has no sprite_map / battle-string slot.
        """
        from .sprite_map_row import _SpriteListPicker  # deferred: avoids cycle
        rec = self._record
        sprite_map = getattr(self._session, "sprite_map", None)
        battle_strings = getattr(self._session, "battle_strings", None)
        if (sprite_map is None or battle_strings is None
                or rec.id >= len(sprite_map) or rec.id >= len(battle_strings)):
            return None
        slot = sprite_map[rec.id]
        str_entry = battle_strings[rec.id]
        self._display_slot = slot

        box = QGroupBox("Display / Reskin")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        form_wrap = QWidget()
        form = make_form(form_wrap)
        pickers = [
            ("Overworld (party)", self._session.get_mchr_labels, "mchr", "unknown_0x4"),
            ("Main sprite", self._session.get_btchr_group_labels, "btchr", "main_sprite"),
            ("Icon Portrait", self._session.get_spr_labels, "spr", "upperscreen_low"),
            ("Battle Mini", self._session.get_spr_labels, "spr", "upperscreen_high"),
        ]
        picker_widgets = []
        for label, provider, kind, attr in pickers:
            picker = _SpriteListPicker(provider, self._undo, shared_kind=kind)
            picker.bind(slot, attr)
            form.addRow(label, picker)
            self._bound.append(picker)
            picker_widgets.append(picker)

        base = constants.STRING_BATTLE_TABLE_OFFSET[self._session.version][0]
        bs_choices = [
            (g.offset - base, g.text)
            for g in self._session.string_regions.get("arm9_digiegg_enemy_names", [])
        ]
        battle_str = BoundIdCombo(
            str_entry, "value", bs_choices, self._undo, shared_kind="battle_strings")
        form.addRow("Battle string", battle_str)
        self._bound.append(battle_str)
        outer.addWidget(form_wrap)

        # Live thumbnails of the four sprites, so B shows its sprites the way
        # the editor's own Display row does — repainted when a picker (or a
        # swap/copy) changes the slot.
        prev_size = 52
        renderers = [
            ("Overworld", "unknown_0x4",
             lambda v: self._session.mchr_sprite_pixmap(v, max_size=prev_size)),
            ("Battle", "main_sprite",
             lambda v: self._session.battle_sprite_pixmap(v, max_size=prev_size)),
            ("Portrait", "upperscreen_low",
             lambda v: self._session.spr_sprite_pixmap(v, max_size=prev_size)),
            ("Mini", "upperscreen_high",
             lambda v: self._session.spr_sprite_pixmap(v, max_size=prev_size)),
        ]
        prev_row = QHBoxLayout()
        prev_row.setContentsMargins(0, 0, 0, 0)
        prev_row.setSpacing(8)
        for caption, attr, render in renderers:
            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(1)
            cap = QLabel(caption)
            cap.setAlignment(Qt.AlignHCenter)
            cap.setStyleSheet("color: palette(mid); font-size: 10px;")
            img = QLabel()
            img.setFixedSize(prev_size, prev_size)
            img.setAlignment(Qt.AlignCenter)
            img.setStyleSheet(
                "QLabel { background: palette(base); border: 1px solid palette(mid); }")
            col.addWidget(cap)
            col.addWidget(img)
            prev_row.addLayout(col)
            self._disp_previews[attr] = (img, render)
        prev_row.addStretch(1)
        outer.addLayout(prev_row)

        for picker in picker_widgets:
            picker.currentIndexChanged.connect(lambda _i: self._refresh_display_previews())
        self._refresh_display_previews()
        return box

    def _refresh_display_previews(self) -> None:
        slot = self._display_slot
        if slot is None:
            return
        for attr, (img, render) in self._disp_previews.items():
            try:
                pm = render(getattr(slot, attr))
            except Exception:  # noqa: BLE001 — bad id / decode failure → dash
                pm = None
            if pm is not None:
                img.setPixmap(pm)
            else:
                img.clear()
                img.setText("—")


class CompareSwapPanel(QWidget):
    """The "B" side of an inline compare/swap, shown beside a base/enemy editor.

    "A" is the record the host editor is currently editing (read live via
    ``get_a_record``). The panel picks a "B" record, renders it as a
    :class:`DigimonCompareForm`, and runs Swap / Copy against A and B. The host
    calls :meth:`sync_a` whenever its selection changes so the button states and
    note track the edited record.
    """

    def __init__(
        self,
        session,
        undo_stack: Optional[QUndoStack],
        a_is_enemy: bool,
        get_a_record,
        parent=None,
    ):
        super().__init__(parent)
        self._session = session
        self._undo = undo_stack
        self._a_is_enemy = a_is_enemy
        self._get_a = get_a_record
        self._b_form: Optional[DigimonCompareForm] = None

        self._build_ui()
        # B defaults to the same type as the edited record.
        with QSignalBlocker(self._b_type):
            self._b_type.setCurrentIndex(1 if a_is_enemy else 0)
        self._populate_b_ids()
        self._rebuild_b()
        if undo_stack is not None:
            undo_stack.indexChanged.connect(self._on_undo_index)

    # ---- UI --------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # Names "A" concretely so the Swap/Copy directions read unambiguously.
        self._a_label = QLabel("")
        self._a_label.setStyleSheet("font-weight: bold;")
        self._a_label.setWordWrap(True)
        root.addWidget(self._a_label)

        sel_row = QHBoxLayout()
        sel_row.setContentsMargins(0, 0, 0, 0)
        sel_row.addWidget(QLabel("Compare with"))
        self._b_type = QComboBox()
        self._b_type.addItem("Base", "base")
        self._b_type.addItem("Enemy", "enemy")
        self._b_type.currentIndexChanged.connect(self._on_b_type_changed)
        self._b_id = QComboBox()
        self._b_id.setMaxVisibleItems(24)
        # Editable + substring completer so the user can type a name / id
        # fragment to filter instead of scrolling ~700 entries. NoInsert +
        # a snap-back on focus-out keep free text from corrupting the value.
        self._b_id.setEditable(True)
        self._b_id.setInsertPolicy(QComboBox.NoInsert)
        completer = QCompleter(self._b_id)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self._b_id.setCompleter(completer)
        self._b_id.currentIndexChanged.connect(lambda _ix: self._rebuild_b())
        line_edit = self._b_id.lineEdit()
        if line_edit is not None:
            line_edit.editingFinished.connect(self._snap_b_id_text)
        sel_row.addWidget(self._b_type)
        sel_row.addWidget(self._b_id, 1)
        root.addLayout(sel_row)

        self._swap_btn = QPushButton("⇄ Swap")
        self._swap_btn.setToolTip(
            "Swap all data (except ids) between the digimon you're editing (A) and B.")
        self._swap_btn.clicked.connect(self._on_swap)
        self._copy_to_b = QPushButton("Copy A → B")
        self._copy_to_b.setToolTip(
            "Overwrite B with the data of the digimon you're editing (B keeps its id).")
        self._copy_to_b.clicked.connect(lambda: self._on_copy(to_b=True))
        self._copy_to_a = QPushButton("Copy B → A")
        self._copy_to_a.setToolTip(
            "Overwrite the digimon you're editing (A) with B's data (A keeps its id).")
        self._copy_to_a.clicked.connect(lambda: self._on_copy(to_b=False))
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.addWidget(self._swap_btn)
        btn_row.addWidget(self._copy_to_b)
        btn_row.addWidget(self._copy_to_a)
        root.addLayout(btn_row)

        self._op_note = QLabel("")
        self._op_note.setStyleSheet("color: #888;")
        self._op_note.setWordWrap(True)
        root.addWidget(self._op_note)

        # The B form scroll is mounted here, rebuilt on each B change.
        self._b_container = QWidget()
        self._b_container_layout = QVBoxLayout(self._b_container)
        self._b_container_layout.setContentsMargins(0, 0, 0, 0)
        self._b_container_layout.setSpacing(0)
        root.addWidget(self._b_container, 1)

    # ---- B selection -----------------------------------------------------

    def _entries_for(self, kind: str) -> dict:
        return self._session.enemy_digimon if kind == "enemy" else self._session.base_digimon

    def _b_is_enemy(self) -> bool:
        return self._b_type.currentData() == "enemy"

    def _scripted_suffix(self, is_enemy: bool, rid: int) -> str:
        """" [Event location]" for a scripted enemy (matches the enemy list),
        or "" for base entries / enemies with no scripted event."""
        if not is_enemy:
            return ""
        loc = scripted_area_label(self._session, rid)
        return f"  [{loc}]" if loc else ""

    def _populate_b_ids(self) -> None:
        is_enemy = self._b_is_enemy()
        entries = self._entries_for(self._b_type.currentData())
        with QSignalBlocker(self._b_id):
            self._b_id.clear()
            for rid in sorted(entries):
                name = self._session.digimon_display_name(rid)
                self._b_id.addItem(
                    f"0x{rid:03x}  {name}{self._scripted_suffix(is_enemy, rid)}", rid)
        completer = self._b_id.completer()
        if completer is not None:
            completer.setModel(self._b_id.model())

    def _snap_b_id_text(self) -> None:
        """Resolve free-typed text on focus-out: exact label match or the sole
        substring match wins; otherwise revert to the current selection so the
        box never holds an unresolved value."""
        line_edit = self._b_id.lineEdit()
        if line_edit is None:
            return
        ix = self._b_id.currentIndex()
        text = line_edit.text().strip()
        if ix >= 0 and text == self._b_id.itemText(ix):
            return
        lowered = text.casefold()
        exact = None
        substrings = []
        for i in range(self._b_id.count()):
            item = self._b_id.itemText(i)
            if item.casefold() == lowered:
                exact = i
                break
            if lowered and lowered in item.casefold():
                substrings.append(i)
        target = exact if exact is not None else (
            substrings[0] if len(substrings) == 1 else None)
        if target is not None and target != ix:
            self._b_id.setCurrentIndex(target)
        elif ix >= 0:
            with QSignalBlocker(line_edit):
                line_edit.setText(self._b_id.itemText(ix))

    def _b_record(self):
        rid = self._b_id.currentData()
        if rid is None:
            return None
        return self._entries_for(self._b_type.currentData()).get(rid)

    def _on_b_type_changed(self, _ix: int) -> None:
        self._populate_b_ids()
        self._rebuild_b()

    def _rebuild_b(self) -> None:
        rec = self._b_record()
        self._b_form = (
            DigimonCompareForm(rec, self._b_is_enemy(), self._undo, self._session)
            if rec is not None else None
        )
        while self._b_container_layout.count():
            old = self._b_container_layout.takeAt(0).widget()
            if old is not None:
                old.deleteLater()
        if self._b_form is not None:
            self._b_container_layout.addWidget(
                wrap_in_scroll(self._b_form, reflow=True), 1)
        else:
            ph = QLabel("(no entry)")
            ph.setAlignment(Qt.AlignCenter)
            ph.setStyleSheet("color: #888;")
            self._b_container_layout.addWidget(ph, 1)
        self._update_ops()

    # ---- host hooks ------------------------------------------------------

    def sync_a(self) -> None:
        """Called by the host editor when its selection (A) changes."""
        self._update_ops()

    def _on_undo_index(self, *_args) -> None:
        # Swap/copy + undo/redo re-parse B in place; re-read its widgets.
        if self._b_form is None:
            return
        try:
            self._b_form.refresh()
        except RuntimeError:
            pass  # form torn down (e.g. rebuilt/closed) before the signal fired

    # ---- ops -------------------------------------------------------------

    def _name(self, rec) -> Optional[str]:
        return self._session.digimon_display_name(rec.id) if rec is not None else None

    def _update_ops(self) -> None:
        a = self._get_a()
        b = self._b_record()
        a_name, b_name = self._name(a), self._name(b)
        if a is not None:
            a_suffix = self._scripted_suffix(self._a_is_enemy, a.id)
            self._a_label.setText(f"Editing (A):  {a_name}{a_suffix}  ·  0x{a.id:03x}")
        else:
            self._a_label.setText("Select a digimon to edit.")
        same = self._a_is_enemy == self._b_is_enemy()
        distinct = a is not None and b is not None and a.id != b.id
        ok = (a is not None and b is not None and same and distinct
              and self._undo is not None)
        for btn in (self._swap_btn, self._copy_to_b, self._copy_to_a):
            btn.setEnabled(ok)
        # Name-based labels so each button's direction is unambiguous, but
        # truncated so a long B name doesn't blow the buttons up. Full names
        # live in the A header + the "Compare with" picker + the tooltips.
        if a_name and b_name:
            sa, sb = _short(a_name), _short(b_name)
            self._swap_btn.setText(f"⇄ Swap {sa} ⇄ {sb}")
            self._copy_to_b.setText(f"Copy {sa} → {sb}")
            self._copy_to_a.setText(f"Copy {sb} → {sa}")
        else:
            self._swap_btn.setText("⇄ Swap")
            self._copy_to_b.setText("Copy A → B")
            self._copy_to_a.setText("Copy B → A")
        if a is None:
            self._op_note.setText("Select a digimon to edit first.")
        elif b is None:
            self._op_note.setText("")
        elif not same:
            kind = "enemy" if self._a_is_enemy else "base"
            self._op_note.setText(
                f"Swap/copy need both to be {kind} entries (same as what you're editing).")
        elif not distinct:
            self._op_note.setText("Pick a different entry to swap/copy with.")
        else:
            self._op_note.setText("Swap/copy include the sprites + name and keep both ids.")

    def _display_slots(self, id_x: int, id_y: int):
        """(sprite_x, sprite_y, str_x, str_y) for two ids, or None if either id
        has no sprite_map / battle-string slot."""
        sm = getattr(self._session, "sprite_map", None)
        bs = getattr(self._session, "battle_strings", None)
        if (sm is None or bs is None or id_x >= len(sm) or id_y >= len(sm)
                or id_x >= len(bs) or id_y >= len(bs)):
            return None
        return sm[id_x], sm[id_y], bs[id_x], bs[id_y]

    def _on_swap(self) -> None:
        a, b = self._get_a(), self._b_record()
        if a is None or b is None or self._undo is None:
            return
        desc = f"Swap {self._name(a)} ⇄ {self._name(b)}"
        # One undo step covering the record data + the display (sprites + name).
        self._undo.beginMacro(desc)
        self._undo.push(SwapDigimonRecordCommand(a, b, desc))
        disp = self._display_slots(a.id, b.id)
        if disp is not None:
            self._undo.push(SwapDisplayCommand(*disp, desc))
        self._undo.endMacro()

    def _on_copy(self, to_b: bool) -> None:
        a, b = self._get_a(), self._b_record()
        if a is None or b is None or self._undo is None:
            return
        dest, src = (b, a) if to_b else (a, b)
        desc = f"Copy {self._name(src)} → {self._name(dest)}"
        self._undo.beginMacro(desc)
        self._undo.push(CopyDigimonRecordCommand(dest, src, desc))
        disp = self._display_slots(dest.id, src.id)
        if disp is not None:
            dest_sprite, src_sprite, dest_str, src_str = disp
            self._undo.push(CopyDisplayCommand(
                dest_sprite, src_sprite, dest_str, src_str, desc))
        self._undo.endMacro()
