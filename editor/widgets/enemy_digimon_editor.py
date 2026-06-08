"""EnemyDataDigimon editor — left list of digimon, right detail form.

Layout mirrors BaseDigimonEditor; EnemyData differs in:
  * traits are 2 bytes (not 1) and there's no support_trait
  * no `evasion` exposure as a "stat" group, but the model still has it
  * `aptitude` is replaced by the per-species exp yields block (8 x 4-byte)
  * extra "unknown" 4-byte tail fields
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from .._perf import span
from .digimon_list_panel import DigimonListPanel
from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundEnumCombo,
    BoundIdCombo,
    BoundIdComboRow,
    BoundSpinBox,
    _make_compact_grid,
    add_unknown_grid_field,
    make_form,
    move_choices,
    trait_choices,
    wrap_in_scroll,
)
from .sprite_map_row import SpriteMapRow, displayed_as_suffix


# Enemy stats often legitimately exceed the engine caps that the base
# (player-side) digimon are clamped to — bosses ship with HP > 9999, etc. So
# no warn_above thresholds here; the cap warnings live only on base digimon.
_STAT_FIELDS: List[Tuple[str, str]] = [
    ("hp", "HP"),
    ("mp", "MP"),
    ("attack", "Attack"),
    ("defense", "Defense"),
    ("spirit", "Spirit"),
    ("speed", "Speed"),
    ("evasion", "Evasion"),
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
]

_MOVE_FIELDS: List[Tuple[str, str]] = [
    ("move_signature", "Signature"),
    ("move_1", "Move 1"),
    ("move_2", "Move 2"),
    ("move_3", "Move 3"),
    ("move_4", "Move 4"),
]

# Per-move usage probability weights (1 byte each at 0x38..0x3B). The engine
# normalises across the four entries, so any combination of values is valid —
# observed totals vary by stage (e.g. 115 for in-training, 220 for some
# rookies). No validation required. Move 4 has no corresponding weight slot.
_USAGE_WEIGHT_FIELDS: List[Tuple[str, str]] = [
    ("usage_weight_signature", "Signature"),
    ("usage_weight_move1", "Move 1"),
    ("usage_weight_move2", "Move 2"),
    ("usage_weight_move3", "Move 3"),
]

_EXP_FIELDS: List[Tuple[str, str]] = [
    ("holy_exp", "Holy"),
    ("dark_exp", "Dark"),
    ("dragon_exp", "Dragon"),
    ("beast_exp", "Beast"),
    ("bird_exp", "Bird"),
    ("machine_exp", "Machine"),
    ("aquan_exp", "Aquan"),
    ("insectplant_exp", "Insect/Plant"),
]

# (attr, label, byte_width, hex_display)
_MISC_FIELDS: List[Tuple[str, str, int, bool]] = [
    ("level", "Level", 1, False),
    ("unknown_0x24", "Unknown 0x24", 2, True),
    ("unknown_0x5C", "Unknown 0x5C", 4, True),
    ("unknown_0x60", "Unknown 0x60", 4, True),
    ("unknown_0x64", "Unknown 0x64", 4, True),
    ("unknown_0x68", "Unknown 0x68", 4, True),
]




class EnemyDigimonEditor(QWidget):
    def __init__(
        self,
        entries: Dict[int, model.EnemyDataDigimon],
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

        with span("EnemyDigimonEditor.__init__"):
            with span("DigimonListPanel"):
                self._list_panel = DigimonListPanel(
                    entries, label_for=self._list_label_for, dirty_aware=True,
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

            undo_stack.indexChanged.connect(self._refresh_form)
            # Reskin commands mutate sprite_map (not the enemy itself), so
            # the list label has to be re-rendered explicitly whenever the
            # undo state advances — _refresh_form only updates the right form.
            undo_stack.indexChanged.connect(self._refresh_list_label_for_current)
            undo_stack.indexChanged.connect(self._list_panel.refresh_dirty_state)
            with span("select_first"):
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

    def _build_detail_container(self) -> QWidget:
        first = next(iter(self._entries.values()))

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        self._id_spin = BoundSpinBox(first, "id", 2, self._undo_stack, hex_display=True, read_only=True)
        self._species_combo = BoundEnumCombo(first, "species", model.Species, self._undo_stack)

        identity_box = QGroupBox("Identity")
        identity_form = make_form(identity_box)
        identity_form.addRow("ID", self._id_spin)
        identity_form.addRow("Species", self._species_combo)

        with span("SpriteMapRow"):
            self._sprite_row = SpriteMapRow(
                self._sprite_map, self._battle_strings, self._undo_stack, self._session,
            )

        self._stat_widgets: Dict[str, BoundSpinBox] = {}
        stats_box = QGroupBox("Stats")
        stats_grid = _make_compact_grid(stats_box, cols=4)
        for ix, (attr, label) in enumerate(_STAT_FIELDS):
            spin = BoundSpinBox(first, attr, 2, self._undo_stack)
            self._stat_widgets[attr] = spin
            stats_grid.addWidget(QLabel(label), ix // 4, (ix % 4) * 2)
            stats_grid.addWidget(spin, ix // 4, (ix % 4) * 2 + 1)

        self._res_widgets: Dict[str, BoundSpinBox] = {}
        res_box = QGroupBox("Resistances")
        res_grid = _make_compact_grid(res_box, cols=4)
        for ix, (attr, label) in enumerate(_RES_FIELDS):
            spin = BoundSpinBox(first, attr, 2, self._undo_stack)
            self._res_widgets[attr] = spin
            res_grid.addWidget(QLabel(label), ix // 4, (ix % 4) * 2)
            res_grid.addWidget(spin, ix // 4, (ix % 4) * 2 + 1)

        # Enemy trait and move fields are both 2 bytes → sentinel 0xFFFF.
        with span("trait_combos"):
            self._trait_rows: Dict[str, BoundIdCombo] = {}
            traits_box = QGroupBox("Traits")
            traits_form = make_form(traits_box)
            for attr, label in _TRAIT_FIELDS:
                combo = BoundIdCombo(
                    first, attr, trait_choices(), self._undo_stack,
                    none_value=0xFFFF, none_label="(none)",
                    shared_kind="traits_word",
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

        self._usage_weight_widgets: Dict[str, BoundSpinBox] = {}
        weights_box = QGroupBox("Move Usage Probabilities")
        weights_form = make_form(weights_box)
        for attr, label in _USAGE_WEIGHT_FIELDS:
            spin = BoundSpinBox(first, attr, 1, self._undo_stack)
            self._usage_weight_widgets[attr] = spin
            weights_form.addRow(label, spin)

        # 4-byte spinboxes are wide; a 4-col grid overruns the panel. 2 cols
        # keeps the form within the windowed width at the cost of 4 rows.
        self._exp_widgets: Dict[str, BoundSpinBox] = {}
        exp_box = QGroupBox("Exp Yield by Tamer Species")
        exp_grid = _make_compact_grid(exp_box, cols=2)
        for ix, (attr, label) in enumerate(_EXP_FIELDS):
            spin = BoundSpinBox(first, attr, 4, self._undo_stack)
            self._exp_widgets[attr] = spin
            exp_grid.addWidget(QLabel(label), ix // 2, (ix % 2) * 2)
            exp_grid.addWidget(spin, ix // 2, (ix % 2) * 2 + 1)
        self._exp_total = QLabel("")
        self._exp_total.setStyleSheet("color: palette(mid);")
        # spans across the 4 columns (2 label+value pairs) of the compact grid
        exp_grid.addWidget(self._exp_total, (len(_EXP_FIELDS) + 1) // 2, 0, 1, 4)

        # Split Misc into a 2-column grid: the 1/2-byte fields (level +
        # 0x24 unknown) stay on the left; the 4-byte 0x5C onward tail unknowns
        # sit on the right so the section doesn't run tall.
        self._misc_widgets: Dict[str, BoundSpinBox] = {}
        misc_box = QGroupBox("Misc")
        misc_grid = _make_compact_grid(misc_box, cols=2)
        split = next(i for i, f in enumerate(_MISC_FIELDS) if f[0] == "unknown_0x5C")
        for row, (attr, label, width, hex_disp) in enumerate(_MISC_FIELDS[:split]):
            spin = BoundSpinBox(first, attr, width, self._undo_stack, hex_display=hex_disp)
            self._misc_widgets[attr] = spin
            if attr.startswith("unknown_"):
                add_unknown_grid_field(misc_grid, row, 0, label, spin)
            else:
                misc_grid.addWidget(QLabel(label), row, 0)
                misc_grid.addWidget(spin, row, 1)
        for row, (attr, label, width, hex_disp) in enumerate(_MISC_FIELDS[split:]):
            spin = BoundSpinBox(first, attr, width, self._undo_stack, hex_display=hex_disp)
            self._misc_widgets[attr] = spin
            if attr.startswith("unknown_"):
                add_unknown_grid_field(misc_grid, row, 1, label, spin)
            else:
                misc_grid.addWidget(QLabel(label), row, 2)
                misc_grid.addWidget(spin, row, 3)

        # Traits and Moves are short forms; pair them in a horizontal row so
        # they share the available width instead of stacking. Move Usage
        # Probabilities sit alongside Moves since they're a per-move companion.
        trait_move_row = QHBoxLayout()
        trait_move_row.setContentsMargins(0, 0, 0, 0)
        trait_move_row.setSpacing(4)
        trait_move_row.addWidget(traits_box, 1)
        trait_move_row.addWidget(moves_box, 1)
        trait_move_row.addWidget(weights_box, 1)

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
        content_layout.addWidget(exp_box)
        content_layout.addWidget(misc_box)
        content_layout.addStretch(1)

        # keep exp total in sync as exp fields change
        for spin in self._exp_widgets.values():
            spin.valueChanged.connect(lambda _v: self._refresh_exp_total())

        with span("wrap_in_scroll"):
            return wrap_in_scroll(content)

    # ---- selection / refresh --------------------------------------------

    def _on_selection(self, digimon_id: int) -> None:
        target = self._entries.get(digimon_id)
        if target is None:
            return
        self._current_id = digimon_id
        self._title.setText(self._title_for(target))

        self._id_spin.rebind(target)
        self._species_combo.rebind(target)
        self._sprite_row.rebind(target)
        for spin in self._stat_widgets.values():
            spin.rebind(target)
        for spin in self._res_widgets.values():
            spin.rebind(target)
        for row in self._trait_rows.values():
            row.rebind(target)
        for row in self._move_rows.values():
            row.rebind(target)
        for spin in self._usage_weight_widgets.values():
            spin.rebind(target)
        for spin in self._exp_widgets.values():
            spin.rebind(target)
        for spin in self._misc_widgets.values():
            spin.rebind(target)
        self._refresh_exp_total()

    def _refresh_form(self, _index: int) -> None:
        target = self._entries.get(self._current_id)
        if target is None:
            return
        self._title.setText(self._title_for(target))
        self._id_spin.refresh()
        self._species_combo.refresh()
        self._sprite_row.refresh()
        for spin in self._stat_widgets.values():
            spin.refresh()
        for spin in self._res_widgets.values():
            spin.refresh()
        for row in self._trait_rows.values():
            row.refresh()
        for row in self._move_rows.values():
            row.refresh()
        for spin in self._usage_weight_widgets.values():
            spin.refresh()
        for spin in self._exp_widgets.values():
            spin.refresh()
        for spin in self._misc_widgets.values():
            spin.refresh()
        self._refresh_exp_total()

    def _refresh_exp_total(self) -> None:
        target = self._entries.get(self._current_id)
        if target is None:
            self._exp_total.setText("")
            return
        self._exp_total.setText(f"Total exp (sum of all species): {target.getTotalExp()}")

    def _list_label_for(self, digimon_id: int) -> str:
        """Decorate the left-pane label.

        Every id with a sprite-map slot picks up a ``[displayed-as]``
        suffix derived from the slot's ``main_sprite`` via the reverse
        sprite-map lookup — so the user can see at a glance which sprite
        each entry actually renders as. For wild-encounter ids the
        suffix usually matches the entry's own name; for reskinned
        fixed enemies it diverges.
        """
        own_name = self._session.digimon_display_name(digimon_id)
        # _build_detail_container() creates _sprite_row, but the list panel
        # asks for labels before that — guard against the bootstrap call.
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

    def _title_for(self, target: model.EnemyDataDigimon) -> str:
        name = self._session.digimon_display_name(target.id)
        suffix = displayed_as_suffix(
            self._sprite_map, self._sprite_row.sprite_to_base, target.id, name,
            name_resolver=self._session.digimon_display_name,
        )
        return f"0x{target.id:03x}  —  {name}{suffix}    (offset 0x{target.offset:08x})"
