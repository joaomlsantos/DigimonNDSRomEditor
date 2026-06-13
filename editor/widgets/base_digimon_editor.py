"""BaseDataDigimon editor — left list of digimon, right detail form."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model
from digimon_core.stat_progression import (
    PROGRESSION_STATS,
    ProgressionMode,
    compute_expected_stats,
    per_level_gain_by_type,
)

from .._perf import span
from .digimon_list_panel import DigimonListPanel
from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundCheckBox,
    BoundEnumCombo,
    BoundIdCombo,
    BoundIdComboRow,
    BoundSpinBox,
    add_unknown_form_row,
    make_form,
    wrap_in_scroll,
    move_choices,
    trait_choices,
)
from .sprite_map_row import SpriteMapRow, displayed_as_suffix


# Game-engine caps enforced by the level-up routine (see research_docs/
# levelup_research.md): HP/MP can't exceed 9999, the rest cap at 999. Set
# higher values here and the engine clamps them on the first level-up. The
# warn_above triggers a red border + tooltip so the user knows.
_HP_MP_CAP = 9999
_PRIMARY_STAT_CAP = 999
_LEVEL_CAP = 99
_CAP_MESSAGE_HP = f"Game engine caps HP/MP at {_HP_MP_CAP}. Higher values get clamped on level-up."
_CAP_MESSAGE_STAT = f"Game engine caps this stat at {_PRIMARY_STAT_CAP}. Higher values get clamped on level-up."
_CAP_MESSAGE_LEVEL = f"Level above {_LEVEL_CAP} is outside the normal in-game range."

# Exp curve is an index into a 3-entry curve table (slow / medium / fast).
# Out-of-range values fall through to the default curve in the engine; the
# warning surfaces this so users editing growth rates know the field can't
# accept arbitrary integers.
_EXP_CURVE_MAX = 2
_CAP_MESSAGE_EXP_CURVE = (
    "Exp curve only accepts 0, 1, or 2 (slow / medium / fast). "
    "Other values fall back to the default curve in the engine."
)

# (attribute, label, byte_width, hex_display, warn_above, warn_message)
_STAT_FIELDS: List[Tuple[str, str, int, bool, int, str]] = [
    ("hp", "HP", 2, False, _HP_MP_CAP, _CAP_MESSAGE_HP),
    ("mp", "MP", 2, False, _HP_MP_CAP, _CAP_MESSAGE_HP),
    ("attack", "Attack", 2, False, _PRIMARY_STAT_CAP, _CAP_MESSAGE_STAT),
    ("defense", "Defense", 2, False, _PRIMARY_STAT_CAP, _CAP_MESSAGE_STAT),
    ("spirit", "Spirit", 2, False, _PRIMARY_STAT_CAP, _CAP_MESSAGE_STAT),
    ("speed", "Speed", 2, False, _PRIMARY_STAT_CAP, _CAP_MESSAGE_STAT),
    ("evasion", "Evasion", 2, False, _PRIMARY_STAT_CAP, _CAP_MESSAGE_STAT),
    ("aptitude", "Aptitude", 2, False, _LEVEL_CAP, _CAP_MESSAGE_LEVEL),
]

_RES_FIELDS: List[Tuple[str, str]] = [
    ("light_res", "Light"),
    ("dark_res", "Dark"),
    ("fire_res", "Fire"),
    ("earth_res", "Earth"),
    ("wind_res", "Wind"),
    ("steel_res", "Steel"),
    ("water_res", "Water"),
    ("thunder_res", "Thunder"),
]

_TRAIT_FIELDS: List[Tuple[str, str]] = [
    ("trait_1", "Trait 1"),
    ("trait_2", "Trait 2"),
    ("trait_3", "Trait 3"),
    ("trait_4", "Trait 4"),
    ("support_trait", "Support"),
]

_MOVE_FIELDS: List[Tuple[str, str]] = [
    ("move_signature", "Signature"),
    ("move_1", "Move 1"),
    ("move_2", "Move 2"),
    ("move_3", "Move 3"),
    ("move_4", "Move 4"),
]

# is_scannable is rendered as a checkbox (see _build_detail_container);
# the rest of the misc block are plain spinboxes.
# (attr, label, byte_width, hex_display, warn_above, warn_message)
# Level lives in the Stats box (it gates the progression formula and
# pairs visually with the six combat stats), not Misc.
_MISC_FIELDS: List[Tuple[str, str, int, bool, Optional[int], str]] = [
    ("dex_habitat", "Dex Habitat", 1, False, None, ""),
    ("exp_curve", "Exp Curve", 4, False, _EXP_CURVE_MAX, _CAP_MESSAGE_EXP_CURVE),
    ("unknown_0x26", "Unknown 0x26", 2, True, None, ""),
    ("unknown_0x38", "Unknown 0x38", 1, True, None, ""),
    ("unknown_0x3A", "Unknown 0x3A", 1, True, None, ""),
]


# Stats whose per-level gain is ≥ this fraction of the cross-type mean
# are colored as a StatType "specialization" in the Growth row. 1.15
# keeps the BALANCE archetype unhighlighted (it's the average by
# construction) while still surfacing each non-BALANCE type's
# signature stat(s). Tuned empirically against LEVEL_UP_TABLE.
_GROWTH_EMPHASIS_RATIO = 1.15


def _cross_type_mean_gains() -> Dict[str, float]:
    """Per-stat mean of average per-level gain across all DigimonType rows.

    Used as the baseline for the Growth row's emphasis: a stat whose
    gain is notably above this mean is what that StatType is for. Cheap
    enough to recompute on demand; if it ever becomes hot we can
    @lru_cache it.
    """
    sums: Dict[str, float] = {name: 0.0 for name in PROGRESSION_STATS}
    n_types = len(constants.LEVEL_UP_TABLE)
    for type_ix in range(n_types):
        for name, gain in per_level_gain_by_type(type_ix).items():
            sums[name] += gain
    return {name: total / n_types for name, total in sums.items()}


class BaseDigimonEditor(QWidget):
    # Matches the dispatch key in main_window._build_editor_for; used to
    # save/restore the cursor across editor switches within a session.
    _CURSOR_KEY = "base_digimon"

    def __init__(
        self,
        entries: Dict[int, model.BaseDataDigimon],
        undo_stack: QUndoStack,
        sprite_map: List[model.SpriteMapEntry],
        battle_strings: List[model.BattleStringEntry],
        session,
        parent=None,
    ):
        super().__init__(parent)
        self._entries = entries
        self._undo_stack = undo_stack
        self._sprite_map = sprite_map
        self._battle_strings = battle_strings
        self._session = session
        self._current_id: int = -1

        with span("BaseDigimonEditor.__init__"):
            with span("DigimonListPanel"):
                # Non-Scannable base records are unused slots — the engine
                # never recruits or scans them, so flag them visually so the
                # user can tell at a glance which rows carry live data.
                self._list_panel = DigimonListPanel(
                    entries,
                    label_for=self._list_label_for,
                    dirty_aware=True,
                    dim_for=lambda did: not bool(
                        getattr(entries.get(did), "is_scannable", 0)
                    ),
                    legend="dim = not Scannable (unused slot)",
                )
            self._list_panel.digimonSelected.connect(self._on_selection)

            # _build_detail_container() creates _sprite_row, which the label
            # callback depends on for the reverse sprite-to-base lookup. The
            # list was built before that existed, so re-render every row now.
            with span("_build_detail_container"):
                self._detail = self._build_detail_container()
            with span("refresh_all_labels"):
                self._list_panel.refresh_all_labels()

            with span("splitter+layout"):
                splitter = QSplitter(Qt.Horizontal, self)
                splitter.addWidget(self._list_panel)
                splitter.addWidget(self._detail)
                splitter.setStretchFactor(0, 0)
                splitter.setStretchFactor(1, 1)
                splitter.setSizes([260, 740])

                layout = QVBoxLayout(self)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.addWidget(splitter)

            # repopulate after undo/redo so the form reflects the model
            undo_stack.indexChanged.connect(self._refresh_form)
            # Reskin commands mutate sprite_map (not the digimon itself), so
            # the list label has to be re-rendered explicitly whenever the
            # undo state advances — _refresh_form only updates the right form.
            undo_stack.indexChanged.connect(self._refresh_list_label_for_current)
            undo_stack.indexChanged.connect(self._list_panel.refresh_dirty_state)

            with span("restore_selection"):
                remembered = self._session.recall_selection(self._CURSOR_KEY)
                if remembered is None or not self._list_panel.select_by_id(int(remembered)):
                    self._list_panel.select_first()

    def select_by_id(self, digimon_id: int) -> bool:
        return self._list_panel.select_by_id(digimon_id)

    def aboutToTeardown(self) -> None:
        """Called by main_window.set_content before this editor is destroyed.

        Hands every pooled widget back to the session — otherwise Qt
        deletes them along with this widget tree, defeating the pool.
        """
        self._sprite_row.release_pickers()
        self._session.release_combo_pool("moves", self._moves_pool_generation)

    # ---- detail form -----------------------------------------------------

    def _build_detail_container(self) -> QWidget:
        first = next(iter(self._entries.values()))

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        self._id_spin = BoundSpinBox(first, "id", 2, self._undo_stack, hex_display=True, read_only=True)
        self._species_combo = BoundEnumCombo(first, "species", model.Species, self._undo_stack)
        # StatType lives in the Stats box next to Level — it pairs with
        # the per-stat values rather than the species/identity row.
        self._type_combo = BoundEnumCombo(first, "digimon_type", model.DigimonType, self._undo_stack)

        identity_box = QGroupBox("Identity")
        identity_form = make_form(identity_box)
        identity_form.addRow("ID", self._id_spin)
        identity_form.addRow("Species", self._species_combo)

        # Reverse link to the enemy editor — counts enemy records that
        # advertise this base's species, plus how many have stats
        # outside the ±10% band the enemy editor uses as its
        # divergence cue. The label is muted because it's an
        # informational footer, not a knob.
        self._used_by_label = QLabel("—")
        self._used_by_label.setStyleSheet("color: palette(mid);")
        self._used_by_label.setWordWrap(True)
        identity_form.addRow("Used by", self._used_by_label)

        with span("SpriteMapRow"):
            self._sprite_row = SpriteMapRow(
                self._sprite_map, self._battle_strings, self._undo_stack, self._session,
            )

        # Stats laid out as a horizontal table — one column per stat,
        # values in a single row underneath the stat-name headers. Mirrors
        # the enemy editor's structure for visual parity. Level drives the
        # progression formula and sits on its own header row above the
        # table, like the enemy editor. Aptitude is kept in the table
        # alongside the combat stats (it gates starter level caps, but
        # visually belongs with the per-stat values).
        self._stat_widgets: Dict[str, BoundSpinBox] = {}
        # Per-level average gain shown muted below each stat — only the
        # StatType drives these, so the row updates whenever the type
        # combo changes (makes the gameplay impact of StatType obvious).
        self._gain_labels: Dict[str, QLabel] = {}
        # Misc-style "level" widget kept in _misc_widgets for callers
        # that still index it by name (the sidecar's live-update hook).
        self._misc_widgets: Dict[str, object] = {}
        stats_box = self._build_stats_box(first)

        self._res_widgets: Dict[str, BoundSpinBox] = {}
        res_box = self._build_resistances_box(first)

        # Sentinel values for "no trait" / "no move" slots — all-bits-set for
        # the field's byte width. Base digimon trait fields are 1 byte → 0xFF;
        # move fields are 2 bytes → 0xFFFF.
        with span("trait_combos"):
            self._trait_rows: Dict[str, BoundIdCombo] = {}
            traits_box = QGroupBox("Traits")
            traits_form = make_form(traits_box)
            for attr, label in _TRAIT_FIELDS:
                combo = BoundIdCombo(
                    first, attr, trait_choices(), self._undo_stack,
                    none_value=0xFF, none_label="(none)",
                    shared_kind="traits_byte",
                )
                self._trait_rows[attr] = combo
                traits_form.addRow(label, combo)

        with span("move_combos"):
            # Pooled at the session level — see RomSession._build_combo_pools.
            # Pool slot order matches _MOVE_FIELDS so attrs line up 1:1.
            pool_rows, self._moves_pool_generation = self._session.acquire_combo_pool("moves")
            self._move_rows: Dict[str, BoundIdComboRow] = {}
            moves_box = QGroupBox("Moves")
            moves_form = make_form(moves_box)
            for (attr, label), row in zip(_MOVE_FIELDS, pool_rows):
                row.set_undo_stack(self._undo_stack)
                row.rebind(first)
                self._move_rows[attr] = row
                moves_form.addRow(label, row)

        misc_box = QGroupBox("Misc")
        misc_form = make_form(misc_box)
        scannable_check = BoundCheckBox(first, "is_scannable", self._undo_stack)
        self._misc_widgets["is_scannable"] = scannable_check
        misc_form.addRow("Scannable", scannable_check)
        for attr, label, width, hex_disp, warn_cap, warn_msg in _MISC_FIELDS:
            spin = BoundSpinBox(
                first, attr, width, self._undo_stack,
                hex_display=hex_disp, warn_above=warn_cap, warn_message=warn_msg,
            )
            self._misc_widgets[attr] = spin
            if attr.startswith("unknown_"):
                add_unknown_form_row(misc_form, label, spin)
            else:
                misc_form.addRow(label, spin)

        # Pair Traits and Moves in a single row to halve the vertical space.
        trait_move_row = QHBoxLayout()
        trait_move_row.setContentsMargins(0, 0, 0, 0)
        trait_move_row.setSpacing(4)
        trait_move_row.addWidget(traits_box, 1)
        trait_move_row.addWidget(moves_box, 1)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.setSpacing(4)
        content_layout.addWidget(self._title)
        content_layout.addWidget(identity_box)
        content_layout.addWidget(self._sprite_row.widget)
        content_layout.addWidget(stats_box)
        content_layout.addWidget(res_box)
        content_layout.addLayout(trait_move_row)
        content_layout.addWidget(misc_box)
        content_layout.addStretch(1)

        # Per-stat edits move the divergence count against the matched
        # enemy entry. Cheaper to refresh just the footer than to redo
        # the whole form refresh.
        for spin in self._stat_widgets.values():
            spin.valueChanged.connect(lambda _v: self._refresh_used_by_label())

        with span("wrap_in_scroll"):
            return wrap_in_scroll(content)

    def _build_stats_box(self, first: model.BaseDataDigimon) -> QWidget:
        """Stats box laid out as a horizontal table — one column per
        stat with the editable value in a single row underneath the
        stat-name header. Level sits on its own header row above the
        table since it gates the progression formula (mirrors the
        enemy editor's layout).
        """
        box = QGroupBox("Stats")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(8)
        top_row.addWidget(QLabel("Level"))
        level_spin = BoundSpinBox(
            first, "level", 1, self._undo_stack,
            warn_above=_LEVEL_CAP, warn_message=_CAP_MESSAGE_LEVEL,
        )
        self._misc_widgets["level"] = level_spin
        top_row.addWidget(level_spin)
        top_row.addSpacing(16)
        top_row.addWidget(QLabel("StatType"))
        top_row.addWidget(self._type_combo)
        top_row.addStretch(1)
        outer.addLayout(top_row)

        table = QGridLayout()
        table.setContentsMargins(0, 4, 0, 0)
        table.setHorizontalSpacing(8)
        table.setVerticalSpacing(2)

        # Column 0 is the row-label column ("Current" / "Growth"),
        # matching the enemy editor's Current/Expected/Range layout.
        # Stat columns start at 1.
        for ix, (attr, label, _w, _h, _cap, _msg) in enumerate(_STAT_FIELDS):
            header = QLabel(label)
            header.setAlignment(Qt.AlignCenter)
            header.setStyleSheet("font-weight: bold;")
            table.addWidget(header, 0, 1 + ix)

        cur_lbl = QLabel("Current")
        cur_lbl.setStyleSheet("color: palette(mid);")
        table.addWidget(cur_lbl, 1, 0)
        for ix, (attr, label, width, hex_disp, warn_cap, warn_msg) in enumerate(_STAT_FIELDS):
            spin = BoundSpinBox(
                first, attr, width, self._undo_stack,
                hex_display=hex_disp, warn_above=warn_cap, warn_message=warn_msg,
            )
            spin.setAlignment(Qt.AlignCenter)
            self._stat_widgets[attr] = spin
            table.addWidget(spin, 1, 1 + ix)

        # Per-level avg gain row — muted, centred. Evasion and Aptitude
        # don't grow, so their cells stay as em-dashes. Whichever stats
        # the active StatType specializes in get an accent color (see
        # _refresh_growth_preview) so the archetype's identity reads at
        # a glance — that's the whole reason to expose the row.
        # Reads "<StatType> Growth" (e.g. "ATTACKER Growth") and
        # re-renders as the combo changes — keeps the connection
        # between archetype and per-level gains explicit at a glance.
        self._growth_row_label = QLabel("Growth")
        self._growth_row_label.setStyleSheet("color: palette(mid);")
        self._growth_row_label.setToolTip(
            "Average stat gain per level under the current StatType. "
            "Specialized stats (notably above the cross-type average) "
            "are highlighted."
        )
        table.addWidget(self._growth_row_label, 2, 0)

        # Pin column 0 to the widest possible label so swapping
        # "<StatType> Growth" text doesn't reflow the spinboxes when
        # switching between digimon with different StatTypes.
        fm = self._growth_row_label.fontMetrics()
        widest = max(
            (fm.horizontalAdvance(f"{t.name} Growth") for t in model.DigimonType),
            default=fm.horizontalAdvance("Current"),
        )
        table.setColumnMinimumWidth(0, widest + 8)
        for ix, (attr, _l, _w, _h, _cap, _msg) in enumerate(_STAT_FIELDS):
            gain_lbl = QLabel("—")
            gain_lbl.setAlignment(Qt.AlignCenter)
            gain_lbl.setStyleSheet("color: palette(mid);")
            self._gain_labels[attr] = gain_lbl
            table.addWidget(gain_lbl, 2, 1 + ix)

        # Anchor flush-left; QGridLayout otherwise even-distributes the
        # leftover horizontal slack across all columns.
        table.setColumnStretch(1 + len(_STAT_FIELDS), 1)
        outer.addLayout(table)

        # Re-render the gain row whenever StatType changes — the combo
        # is the sole input, so this is the cheapest live-update hook.
        self._type_combo.currentIndexChanged.connect(
            lambda _ix: self._refresh_growth_preview()
        )
        return box

    def _refresh_growth_preview(self) -> None:
        """Update the per-level-gain row from the current StatType.

        StatType is the sole driver of growth, so this reads
        ``self._type_combo`` rather than the bound record (the combo's
        value is what BoundEnumCombo pushed onto the record; either
        source agrees, but the combo is cheaper to query and works
        before the first ``rebind``). Stats without a growth curve
        (Evasion, Aptitude) stay as em-dashes. Stats this StatType
        specializes in (gain notably above the cross-type average for
        that stat — see :data:`_GROWTH_EMPHASIS_RATIO`) get an accent
        color so the archetype's identity is readable at a glance.
        """
        type_value = self._type_combo.currentData()
        if type_value is None:
            self._growth_row_label.setText("StatType Growth")
            for lbl in self._gain_labels.values():
                lbl.setText("—")
                lbl.setStyleSheet("color: palette(mid);")
            return
        # BoundEnumCombo stores the Enum member as item data; unwrap to
        # its underlying int for the LEVEL_UP_TABLE row index.
        type_ix = type_value.value if hasattr(type_value, "value") else int(type_value)
        type_name = (
            type_value.name if hasattr(type_value, "name")
            else self._type_combo.currentText()
        )
        self._growth_row_label.setText(f"{type_name} Growth")
        gains = per_level_gain_by_type(int(type_ix))
        baselines = _cross_type_mean_gains()
        for attr, lbl in self._gain_labels.items():
            avg = gains.get(attr)
            if avg is None:
                lbl.setText("—")
                lbl.setStyleSheet("color: palette(mid);")
                continue
            lbl.setText(f"+{avg:.1f}")
            baseline = baselines.get(attr, 0.0)
            if baseline > 0 and avg >= baseline * _GROWTH_EMPHASIS_RATIO:
                # Soft green — semantically "specialization" / above the
                # crowd. Bold so the emphasized cells pop without making
                # the unemphasized ones look washed out.
                lbl.setStyleSheet("color: #3d8b3d; font-weight: bold;")
            else:
                lbl.setStyleSheet("color: palette(mid);")

    def _build_resistances_box(self, first: model.BaseDataDigimon) -> QWidget:
        """Resistance table — column per element, single editable row.

        Mirrors the enemy editor's resistance table without the Base
        comparison row (this *is* the base record).
        """
        box = QGroupBox("Resistances")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        table = QGridLayout()
        table.setContentsMargins(0, 0, 0, 0)
        table.setHorizontalSpacing(8)
        table.setVerticalSpacing(2)

        for ix, (attr, label) in enumerate(_RES_FIELDS):
            header = QLabel(label)
            header.setAlignment(Qt.AlignCenter)
            header.setStyleSheet("font-weight: bold;")
            table.addWidget(header, 0, ix)

        for ix, (attr, label) in enumerate(_RES_FIELDS):
            spin = BoundSpinBox(first, attr, 2, self._undo_stack)
            spin.setAlignment(Qt.AlignCenter)
            self._res_widgets[attr] = spin
            table.addWidget(spin, 1, ix)

        table.setColumnStretch(len(_RES_FIELDS), 1)
        outer.addLayout(table)
        return box

    # ---- selection / refresh --------------------------------------------

    def _on_selection(self, digimon_id: int) -> None:
        target = self._entries.get(digimon_id)
        if target is None:
            return
        self._current_id = digimon_id
        self._session.remember_selection(self._CURSOR_KEY, digimon_id)
        self._title.setText(self._title_for(target))

        self._id_spin.rebind(target)
        self._species_combo.rebind(target)
        self._type_combo.rebind(target)
        self._sprite_row.rebind(target)
        for spin in self._stat_widgets.values():
            spin.rebind(target)
        for spin in self._res_widgets.values():
            spin.rebind(target)
        for row in self._trait_rows.values():
            row.rebind(target)
        for row in self._move_rows.values():
            row.rebind(target)
        for spin in self._misc_widgets.values():
            spin.rebind(target)
        # BoundEnumCombo.rebind silences signals, so the auto-hook
        # we set up in _build_stats_box doesn't fire here.
        self._refresh_growth_preview()
        self._refresh_used_by_label()

    def _refresh_form(self, _index: int) -> None:
        target = self._entries.get(self._current_id)
        if target is None:
            return
        self._title.setText(self._title_for(target))
        self._id_spin.refresh()
        self._species_combo.refresh()
        self._type_combo.refresh()
        self._sprite_row.refresh()
        for spin in self._stat_widgets.values():
            spin.refresh()
        for spin in self._res_widgets.values():
            spin.refresh()
        for row in self._trait_rows.values():
            row.refresh()
        for row in self._move_rows.values():
            row.refresh()
        for spin in self._misc_widgets.values():
            spin.refresh()
        # Undo/redo of a StatType change won't fire the combo's signal
        # (refresh() runs silenced); refresh the gain row explicitly so
        # the preview tracks the undo stack.
        self._refresh_growth_preview()
        self._refresh_used_by_label()

    def _refresh_used_by_label(self) -> None:
        """Recompute the "Enemy entry: in sync / N stats diverge" line.

        Enemy and base records share an id space (per the sprite-map
        memory note), so the link is 1:1 — there's at most one enemy
        entry for each base id. Divergence counts how many of the six
        progression stats fall outside the ±10% band when the level-up
        formula is walked from the base at FIXED_AVG. HP buff is
        intentionally *not* applied here so the count doesn't depend on
        the enemy-editor's display toggle.
        """
        target = self._entries.get(self._current_id)
        if target is None:
            self._used_by_label.setText("—")
            return
        enemy = self._session.enemy_digimon.get(self._current_id)
        if enemy is None:
            self._used_by_label.setText("No matching enemy entry")
            return
        avg = compute_expected_stats(
            target, int(enemy.level),
            mode=ProgressionMode.FIXED_AVG,
            apply_hp_buff=False,
        )
        diverge = 0
        for stat in PROGRESSION_STATS:
            expected = avg.stats[stat]
            if expected <= 0:
                continue
            actual = int(getattr(enemy, stat))
            if abs(actual - expected) / expected > 0.10:
                diverge += 1
        if diverge == 0:
            self._used_by_label.setText(
                f"Enemy entry @ L{int(enemy.level)}: in sync with base"
            )
        else:
            self._used_by_label.setText(
                f"Enemy entry @ L{int(enemy.level)}: "
                f"{diverge}/{len(PROGRESSION_STATS)} stats diverge from base"
            )

    def _list_label_for(self, digimon_id: int) -> str:
        """Decorate the left-pane label with a [displayed-as] suffix.

        Same logic as the enemy editor — surfaces reskinned slots at a
        glance. For base species ids the suffix usually matches the
        entry's own name; if the slot has been reskinned (or carries
        an unmapped sprite), it diverges.
        """
        own_name = self._session.digimon_display_name(digimon_id)
        sprite_row = getattr(self, "_sprite_row", None)
        sprite_to_base = sprite_row.sprite_to_base if sprite_row else {}
        suffix = displayed_as_suffix(
            self._sprite_map, sprite_to_base, digimon_id, own_name,
            name_resolver=self._session.digimon_display_name,
        )
        return f"0x{digimon_id:03x} — {own_name}{suffix}"

    def _refresh_list_label_for_current(self, _index: int = 0) -> None:
        if self._current_id < 0:
            return
        self._list_panel.refresh_label(self._current_id)

    def _title_for(self, target: model.BaseDataDigimon) -> str:
        name = self._session.digimon_display_name(target.id)
        suffix = displayed_as_suffix(
            self._sprite_map, self._sprite_row.sprite_to_base, target.id, name,
            name_resolver=self._session.digimon_display_name,
        )
        return f"0x{target.id:03x}  —  {name}{suffix}    (offset 0x{target.offset:08x})"


# ---- aggregated validation collector -------------------------------------
# Reads from the model (not from the active editor's widgets) so the footer
# registry stays correct whether the BaseDigimonEditor is currently open or
# not. Same invariants as the on-screen warn states; one issue per record per
# rule rather than per offending slot, since the popup row already names the
# record and lists which slots in the message body would just be redundant.
from .validation import ValidationIssue  # noqa: E402 — bottom-of-file utility


def base_digimon_issues(entries: Dict[int, model.BaseDataDigimon]) -> List[ValidationIssue]:
    """Footer-level issues for base digimon.

    Mirrors the per-widget red-border caps so anything highlighted on the
    active record also shows up in the footer list when other editors are
    open. One issue per (record, field) breach.
    """
    issues: List[ValidationIssue] = []
    for did, rec in entries.items():
        name = constants.DIGIMON_ID_TO_STR.get(did, f"0x{did:x}")
        for attr, label, _w, _h, cap, _msg in _STAT_FIELDS:
            value = getattr(rec, attr)
            if cap is not None and value > cap:
                issues.append(ValidationIssue(
                    section="Base Digimon",
                    category=label,
                    message=f"{name} — {label} {value} exceeds the cap of {cap}.",
                    editor_key="base_digimon",
                    record_id=did,
                ))
        # Level lives in the Stats box visually but the widget table
        # doesn't carry it — apply the cap inline.
        if rec.level > _LEVEL_CAP:
            issues.append(ValidationIssue(
                section="Base Digimon",
                category="Level",
                message=f"{name} — Level {rec.level} exceeds the cap of {_LEVEL_CAP}.",
                editor_key="base_digimon",
                record_id=did,
            ))
        for attr, label, _w, _h, cap, _msg in _MISC_FIELDS:
            if cap is None:
                continue
            value = getattr(rec, attr)
            if value > cap:
                issues.append(ValidationIssue(
                    section="Base Digimon",
                    category=label,
                    message=f"{name} — {label} {value} exceeds the cap of {cap}.",
                    editor_key="base_digimon",
                    record_id=did,
                ))
    return issues
