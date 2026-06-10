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
    QCheckBox,
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
    expected_range,
)

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

# Level lives in the Stats box (drives the sidecar's progression
# preview); kept out of Misc to put it near the stats it gates.
# (attr, label, byte_width, hex_display)
_MISC_FIELDS: List[Tuple[str, str, int, bool]] = [
    ("unknown_0x24", "Unknown 0x24", 2, True),
    ("unknown_0x5C", "Unknown 0x5C", 4, True),
    ("unknown_0x60", "Unknown 0x60", 4, True),
    ("unknown_0x64", "Unknown 0x64", 4, True),
    ("unknown_0x68", "Unknown 0x68", 4, True),
]




class EnemyDigimonEditor(QWidget):
    # Matches the dispatch key in main_window._build_editor_for; used to
    # save/restore the cursor across editor switches within a session.
    _CURSOR_KEY = "enemy_digimon"

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
                # An enemy slot whose base record is Scannable is wild-encounter
                # data; everything else is either a fixed-enemy slot or unused.
                # Both kinds carry live data we care about, so flag the wild
                # rows with a marker instead of dimming the others — keeps
                # everything equally readable while making the partition
                # obvious at a glance.
                base_records = session.base_digimon
                self._list_panel = DigimonListPanel(
                    entries,
                    label_for=self._list_label_for,
                    dirty_aware=True,
                    mark_for=lambda did: bool(
                        getattr(base_records.get(did), "is_scannable", 0)
                    ),
                    legend="▸ = Wild Encounter slot (Scannable on base)",
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

        # Unified Stats table: one column per stat with the current
        # value on top and the expected (from the matching base's
        # level-up curve) directly underneath, so divergence reads as
        # vertical misalignment instead of a sidecar lookup. Level + HP
        # buff toggle sit above the table since they drive the
        # expected row. Evasion has no level-up curve, so its expected
        # / range cells stay as em-dash placeholders.
        self._stat_widgets: Dict[str, BoundSpinBox] = {}
        self._expected_value_labels: Dict[str, QLabel] = {}
        self._expected_range_labels: Dict[str, QLabel] = {}
        # Level kept in _misc_widgets for callers that index it by name
        # (the expected-stats live-update hook).
        self._misc_widgets: Dict[str, BoundSpinBox] = {}
        stats_box = self._build_stats_with_expected_box(first)

        self._res_widgets: Dict[str, BoundSpinBox] = {}
        self._base_res_labels: Dict[str, QLabel] = {}
        res_box = self._build_resistances_box(first)

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

        # Split Misc into a 2-column grid: the 2-byte 0x24 unknown stays
        # on the left; the 4-byte 0x5C onward tail unknowns sit on the
        # right so the section doesn't run tall.
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

        # Sidecar live-update hooks. Level drives *what* the formula
        # computes (the base record is fixed by id and can't be
        # reassigned from this editor); per-stat actuals only affect
        # the divergence color cue, so they get a lighter handler.
        self._misc_widgets["level"].valueChanged.connect(
            lambda _v: self._refresh_expected_stats(),
        )
        for spin in self._stat_widgets.values():
            spin.valueChanged.connect(lambda _v: self._refresh_divergence_cues())
        # Resistance edits move the Base-row mismatch highlight even
        # though the base record itself is unchanged.
        for spin in self._res_widgets.values():
            spin.valueChanged.connect(lambda _v: self._refresh_base_resistances())

        with span("wrap_in_scroll"):
            return wrap_in_scroll(content)

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
        self._refresh_expected_stats()
        self._refresh_base_resistances()

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
        self._refresh_expected_stats()
        self._refresh_base_resistances()

    def _refresh_exp_total(self) -> None:
        target = self._entries.get(self._current_id)
        if target is None:
            self._exp_total.setText("")
            return
        self._exp_total.setText(f"Total exp (sum of all species): {target.getTotalExp()}")

    def _resolve_reference_base(
        self, target: model.EnemyDataDigimon | None,
    ) -> Tuple[model.BaseDataDigimon | None, int]:
        """Pick the base record to compare this enemy's data against.

        Three-step cascade:
          1. Native slot — if ``base[target.id]`` is Scannable, that's the
             enemy's own species data.
          2. Sprite owner — else look up the base id that originally
             owned ``sprite_map[target.id].main_sprite`` (via the
             ``sprite_to_base`` reverse map). If that base is Scannable,
             the slot is a reskin of a real species and that species's
             stats are the meaningful comparison.
          3. Otherwise no reference — likely a hand-tuned fixed enemy
             with no canonical equivalent. Callers show "—".

        Returns ``(base, owner_id)``. ``owner_id`` is the id of the base
        that was chosen (== ``target.id`` for the native case, a
        different id for the sprite case, ``-1`` when no match).
        """
        if target is None:
            return None, -1
        own_id = int(target.id)
        own_base = self._session.base_digimon.get(own_id)
        if own_base is not None and getattr(own_base, "is_scannable", 0):
            return own_base, own_id
        sprite_row = getattr(self, "_sprite_row", None)
        if sprite_row is None or own_id >= len(self._sprite_map):
            return None, -1
        sprite_value = self._sprite_map[own_id].main_sprite
        owner_id = sprite_row.sprite_to_base.get(sprite_value, -1)
        if owner_id < 0 or owner_id == own_id:
            return None, -1
        owner_base = self._session.base_digimon.get(owner_id)
        if owner_base is not None and getattr(owner_base, "is_scannable", 0):
            return owner_base, owner_id
        return None, -1

    def _refresh_base_resistances(self) -> None:
        """Populate the resistance table's Base row from the matched
        base record. Falls back to "—" if no base maps to this enemy id.

        Mismatches against the Current row are colored to encode
        direction: amber when the base is higher than current, steel
        blue when lower. Both are muted (lower-saturation analogues of
        warning red / informational blue) so a glance reads "this is
        off" without the row demanding attention.
        """
        target = self._entries.get(self._current_id)
        base, _owner_id = self._resolve_reference_base(target)
        for attr, _ in _RES_FIELDS:
            label = self._base_res_labels[attr]
            if base is None or target is None:
                label.setText("—")
                label.setStyleSheet("")
                continue
            base_val = int(getattr(base, attr))
            cur_val = int(getattr(target, attr))
            label.setText(str(base_val))
            if base_val > cur_val:
                label.setStyleSheet("color: #c47b00; font-weight: bold;")
            elif base_val < cur_val:
                label.setStyleSheet("color: #3d7a9b; font-weight: bold;")
            else:
                label.setStyleSheet("")

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

    # ---- expected-stats sidecar -----------------------------------------

    # Stat rows mirror PROGRESSION_STATS exactly. Evasion is in
    # ``_STAT_FIELDS`` but has no level-up curve, so it's intentionally
    # skipped here — the sidecar would have nothing meaningful to show
    # for it.
    _SIDECAR_STAT_LABELS = (
        ("hp", "HP"),
        ("mp", "MP"),
        ("attack", "Atk"),
        ("defense", "Def"),
        ("spirit", "Spr"),
        ("speed", "Spd"),
    )

    # ±10% per-stat divergence band agreed with the user — broad enough
    # that a deliberately-tuned boss stat doesn't always trip the cue,
    # narrow enough that a stat 20% off the curve is visually obvious.
    _DIVERGENCE_BAND = 0.10

    def _build_stats_with_expected_box(self, first: model.EnemyDataDigimon) -> QWidget:
        """Construct the unified Stats box: current + expected rows.

        Layout (top to bottom):
          * Level spinbox + HP-buff checkbox on a shared header row
          * Base-species attribution line (muted)
          * Stat table — one column per stat with rows:
              Current   [spin]   [spin]   ...
              Expected   28       25      ...
              Range    (20-35)  (20-30)  ...

        Evasion is in :data:`_STAT_FIELDS` but has no level-up curve,
        so its expected/range cells stay as em-dash placeholders.
        """
        box = QGroupBox("Stats")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        # Top row: just Level (drives the formula walk). The HP buff
        # toggle is parked below the table — it's a per-editor display
        # knob the user reaches for less often than Level itself.
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(8)
        top_row.addWidget(QLabel("Level"))
        level_spin = BoundSpinBox(first, "level", 1, self._undo_stack)
        self._misc_widgets["level"] = level_spin
        top_row.addWidget(level_spin)
        top_row.addStretch(1)
        outer.addLayout(top_row)

        # Base attribution line — shows which base record + level the
        # expected row is being walked from. Muted because it's
        # informational, not editable.
        self._expected_header = QLabel("—")
        self._expected_header.setStyleSheet("color: palette(mid);")
        self._expected_header.setWordWrap(True)
        outer.addWidget(self._expected_header)

        # Stat table. Column 0 is the row label ("Current" / "Expected" /
        # "Range"); columns 1..N are stats in _STAT_FIELDS order.
        table = QGridLayout()
        table.setContentsMargins(0, 4, 0, 0)
        table.setHorizontalSpacing(8)
        table.setVerticalSpacing(2)

        # Row 0 leaves the (0,0) corner empty; header labels start at col 1.
        for ix, (attr, label) in enumerate(_STAT_FIELDS):
            col = 1 + ix
            header = QLabel(label)
            header.setAlignment(Qt.AlignCenter)
            header.setStyleSheet("font-weight: bold;")
            table.addWidget(header, 0, col)

        # Row 1: "Current" row of editable spinboxes.
        cur_lbl = QLabel("Current")
        cur_lbl.setStyleSheet("color: palette(mid);")
        table.addWidget(cur_lbl, 1, 0)
        for ix, (attr, label) in enumerate(_STAT_FIELDS):
            spin = BoundSpinBox(first, attr, 2, self._undo_stack)
            # Centre the displayed value so it lines up with the
            # centred Expected / Range labels in the column underneath.
            spin.setAlignment(Qt.AlignCenter)
            self._stat_widgets[attr] = spin
            table.addWidget(spin, 1, 1 + ix)

        # Row 2: expected-avg readout. Evasion has no curve → "—".
        exp_lbl = QLabel("Expected")
        exp_lbl.setStyleSheet("color: palette(mid);")
        table.addWidget(exp_lbl, 2, 0)
        for ix, (attr, label) in enumerate(_STAT_FIELDS):
            value_lbl = QLabel("—")
            value_lbl.setAlignment(Qt.AlignCenter)
            self._expected_value_labels[attr] = value_lbl
            table.addWidget(value_lbl, 2, 1 + ix)

        # Row 3: (min–max) for the same column. Muted, smaller-feeling.
        rng_lbl = QLabel("Range")
        rng_lbl.setStyleSheet("color: palette(mid);")
        table.addWidget(rng_lbl, 3, 0)
        for ix, (attr, label) in enumerate(_STAT_FIELDS):
            range_lbl = QLabel("")
            range_lbl.setAlignment(Qt.AlignCenter)
            range_lbl.setStyleSheet("color: palette(mid);")
            self._expected_range_labels[attr] = range_lbl
            table.addWidget(range_lbl, 3, 1 + ix)

        # Without a trailing stretch column, QGridLayout distributes
        # leftover horizontal space evenly across all columns, leaving
        # a visible gap between the row-label column and the first stat.
        # Anchor the table left by pushing the slack into a phantom
        # column past the last stat.
        table.setColumnStretch(1 + len(_STAT_FIELDS), 1)
        outer.addLayout(table)

        # HP buff toggle parked below the table — affects what the
        # Expected / Range rows show but isn't a per-encounter knob the
        # user fiddles with often. Off by default since vanilla
        # authoring doesn't expect the randomizer multiplier.
        self._hp_buff_check = QCheckBox("Estimate expected HP with stage multiplier")
        self._hp_buff_check.setToolTip(
            "Multiplies the expected HP by the matching stage's wild-HP "
            "buff factor (Champion ×3, Ultimate ×4, Mega ×5). Mirrors "
            "the DWDDRandomizer 'WILD_DIGIMON_HP_BUFF_BY_STAGE' option."
        )
        self._hp_buff_check.toggled.connect(
            lambda _on: self._refresh_expected_stats(),
        )
        outer.addWidget(self._hp_buff_check)
        return box

    def _build_resistances_box(self, first: model.EnemyDataDigimon) -> QWidget:
        """Resistance table — mirrors the Stats table's column layout.

        Columns are the eight elemental resistances in :data:`_RES_FIELDS`
        order; rows are the editable enemy value (Current) and the
        matching base record's value (Base). No Expected / Range — these
        are flat per-element values, not a level-derived curve.
        """
        box = QGroupBox("Resistances")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        table = QGridLayout()
        table.setContentsMargins(0, 0, 0, 0)
        table.setHorizontalSpacing(8)
        table.setVerticalSpacing(2)

        # Header row.
        for ix, (attr, label) in enumerate(_RES_FIELDS):
            header = QLabel(label)
            header.setAlignment(Qt.AlignCenter)
            header.setStyleSheet("font-weight: bold;")
            table.addWidget(header, 0, 1 + ix)

        # Current row.
        cur_lbl = QLabel("Current")
        cur_lbl.setStyleSheet("color: palette(mid);")
        table.addWidget(cur_lbl, 1, 0)
        for ix, (attr, label) in enumerate(_RES_FIELDS):
            spin = BoundSpinBox(first, attr, 2, self._undo_stack)
            spin.setAlignment(Qt.AlignCenter)
            self._res_widgets[attr] = spin
            table.addWidget(spin, 1, 1 + ix)

        # Base row — read-only readout from the matching base record.
        base_lbl = QLabel("Base")
        base_lbl.setStyleSheet("color: palette(mid);")
        table.addWidget(base_lbl, 2, 0)
        for ix, (attr, label) in enumerate(_RES_FIELDS):
            value_lbl = QLabel("—")
            value_lbl.setAlignment(Qt.AlignCenter)
            self._base_res_labels[attr] = value_lbl
            table.addWidget(value_lbl, 2, 1 + ix)

        # Match the Stats table — anchor flush-left with a trailing
        # stretch column so QGridLayout doesn't even-distribute slack.
        table.setColumnStretch(1 + len(_RES_FIELDS), 1)
        outer.addLayout(table)
        return box

    def _refresh_expected_stats(self) -> None:
        """Recompute the expected stats panel for the current enemy."""
        target = self._entries.get(self._current_id)
        if target is None:
            self._clear_expected_panel("Select an enemy to preview.")
            return
        base, owner_id = self._resolve_reference_base(target)
        if base is None:
            self._clear_expected_panel(
                "No canonical base — comparison unavailable."
            )
            return
        target_level = int(target.level)
        avg = compute_expected_stats(
            base, target_level,
            mode=ProgressionMode.FIXED_AVG,
            apply_hp_buff=self._hp_buff_check.isChecked(),
        )
        ranges = expected_range(
            base, target_level,
            apply_hp_buff=self._hp_buff_check.isChecked(),
        )
        # Header reads "<base name> @ L<level> (<stage>)" so users can
        # tell at a glance which base the formula's walking, and what
        # stage the HP buff would apply. When the resolver fell through
        # to the sprite owner (non-Scannable own slot), call that out so
        # the user knows the comparison is against a reskin source, not
        # the enemy's own base record.
        base_name = self._session.digimon_display_name(base.id)
        stage_note = f" — {avg.stage}" if avg.stage else ""
        if owner_id == int(target.id):
            attribution = f"Base: {base_name}"
        else:
            attribution = f"Base via sprite: 0x{owner_id:03x} {base_name}"
        self._expected_header.setText(
            f"{attribution} L{base.level} → enemy L{target_level}"
            f"{stage_note}"
        )
        for attr, _ in self._SIDECAR_STAT_LABELS:
            avg_v = avg.stats[attr]
            lo, _mid, hi = ranges[attr]
            self._expected_value_labels[attr].setText(str(avg_v))
            self._expected_range_labels[attr].setText(f"({lo}–{hi})")
        self._refresh_divergence_cues()

    def _clear_expected_panel(self, header_text: str) -> None:
        """Reset the expected rows to the empty state with a footer note."""
        self._expected_header.setText(header_text)
        for attr, _ in self._SIDECAR_STAT_LABELS:
            self._expected_value_labels[attr].setText("—")
            self._expected_range_labels[attr].setText("")
            self._expected_value_labels[attr].setStyleSheet("")

    def _refresh_divergence_cues(self) -> None:
        """Soft color cue: actual vs expected-avg, per stat.

        Off-band stats get a directional color on the expected-value
        label, matching the resistance Base row's convention: amber
        when the reference value (Expected) sits higher than Current,
        steel blue when lower. Within ±_DIVERGENCE_BAND the label
        stays the default colour. The actual spinbox already uses
        ``warn_above`` styling for cap warnings, so painting it here
        would conflict.
        """
        target = self._entries.get(self._current_id)
        if target is None:
            return
        base, _owner_id = self._resolve_reference_base(target)
        if base is None:
            return
        avg = compute_expected_stats(
            base, int(target.level),
            mode=ProgressionMode.FIXED_AVG,
            apply_hp_buff=self._hp_buff_check.isChecked(),
        )
        for attr, _ in self._SIDECAR_STAT_LABELS:
            expected = avg.stats[attr]
            actual = int(getattr(target, attr))
            if expected <= 0:
                # Avoid div-by-zero on degenerate base data; just clear.
                self._expected_value_labels[attr].setStyleSheet("")
                continue
            ratio = abs(actual - expected) / expected
            if ratio <= self._DIVERGENCE_BAND:
                self._expected_value_labels[attr].setStyleSheet("")
            elif expected > actual:
                self._expected_value_labels[attr].setStyleSheet(
                    "color: #c47b00; font-weight: bold;"
                )
            else:
                self._expected_value_labels[attr].setStyleSheet(
                    "color: #3d7a9b; font-weight: bold;"
                )

    def _title_for(self, target: model.EnemyDataDigimon) -> str:
        name = self._session.digimon_display_name(target.id)
        suffix = displayed_as_suffix(
            self._sprite_map, self._sprite_row.sprite_to_base, target.id, name,
            name_resolver=self._session.digimon_display_name,
        )
        return f"0x{target.id:03x}  —  {name}{suffix}    (offset 0x{target.offset:08x})"
